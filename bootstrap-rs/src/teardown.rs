//! Teardown: the port of `teardown.sh` (issue #100).
//!
//! Destroys everything the bootstrap creates, in the script's reverse
//! order, with the same guards and refusal messages. The post-#79
//! semantics are resolved here: after the pivot the CAPI controllers run
//! on the self-managed management cluster, so the "controller host" the
//! deletion guard protects is discovered (kind pre-pivot vs the mgmt
//! kubeconfig post-pivot) instead of assumed to be kind.
//!
//! Best-effort rules mirror the script: every AWS cleanup unit skips
//! gracefully when its resource is absent and never aborts the run on a
//! single failure; the steps that gate host deletion (Flux suspension,
//! workload cluster deletion, deprovision wait) are strict.

use anyhow::{bail, Context, Result};
use std::path::PathBuf;
use std::process::Stdio;
use tokio::process::Command;

use crate::{
    capture_lossy, kubectl_cmd, run, run_quiet, select_toolbox_kind_kubeconfig,
    toolbox_join_kind_network, toolbox_leave_kind_network, Config,
};

// ── Configuration knobs (teardown.sh `${VAR:-default}` equivalents) ───────────

const DEFAULT_CLUSTER_DELETE_TIMEOUT_SECS: u64 = 1200;
const DEFAULT_PROVIDER_DELETE_TIMEOUT_SECS: u64 = 300;

/// Resolved teardown knobs (env-only, like the script's interface).
#[derive(Debug)]
pub struct TeardownConfig {
    /// AWS_ONLY=1: orphan sweep only, no k8s steps.
    pub aws_only: bool,
    /// FORCE_KIND_DELETE=1: remove the controller host unconditionally.
    pub force_host_delete: bool,
    pub cluster_delete_timeout: u64,
    pub provider_delete_timeout: u64,
    /// $HOME-relative default for the post-pivot mgmt kubeconfig.
    pub mgmt_kubeconfig: PathBuf,
}

impl TeardownConfig {
    /// Resolve from the process environment with `${VAR:-default}`
    /// semantics (empty behaves like unset).
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> Result<Self> {
        let value = |name: &str| get(name).filter(|v| !v.is_empty());
        let flag = |name: &str, default: &str| value(name).map(|v| v == default).unwrap_or(false);
        let num = |name: &str, default: u64| -> Result<u64> {
            match value(name) {
                None => Ok(default),
                Some(raw) => raw
                    .parse::<u64>()
                    .with_context(|| format!("{name} must be a number of seconds")),
            }
        };
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .context("HOME is not set; cannot locate the management kubeconfig")?;
        let mgmt_kubeconfig = value("MGMT_KUBECONFIG")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".kube/krops-mgmt.yaml"));
        Ok(Self {
            // Literal "1" enables (shell boolean gate parity).
            aws_only: flag("AWS_ONLY", "1"),
            force_host_delete: flag("FORCE_KIND_DELETE", "1"),
            cluster_delete_timeout: num(
                "CLUSTER_DELETE_TIMEOUT",
                DEFAULT_CLUSTER_DELETE_TIMEOUT_SECS,
            )?,
            provider_delete_timeout: num(
                "PROVIDER_DELETE_TIMEOUT",
                DEFAULT_PROVIDER_DELETE_TIMEOUT_SECS,
            )?,
            mgmt_kubeconfig,
        })
    }
}

// ── Controller-host discovery (the post-#79 resolution) ───────────────────────

/// Where the CAPI controllers (the thing the deletion guard protects)
/// are running. Discovered, not assumed: pre-pivot they live in the kind
/// bootstrap cluster; post-pivot in the self-managed management cluster.
#[derive(Debug, PartialEq)]
pub enum ControllerHost {
    /// The kind bootstrap cluster (`kind-mgmt` context).
    Kind,
    /// The self-managed management cluster, via its exported kubeconfig.
    SelfManaged,
    /// No reachable controller host: everything must go via the AWS
    /// orphan sweep (AWS_ONLY semantics even when not requested).
    Unreachable,
}

/// A concrete Kubernetes target: the kind bootstrap context (pre-pivot) or
/// the self-managed mgmt kubeconfig (post-pivot). Neither relies on the
/// caller's `kubectl` current-context, so a stale `use-context` cannot
/// redirect a destructive operation (issue #247).
#[derive(Debug, Clone, PartialEq)]
pub enum K8sTarget {
    /// Pre-pivot: the kind bootstrap cluster, addressed by context name.
    Kind { context: String },
    /// Post-pivot: the self-managed management cluster, by kubeconfig path.
    SelfManaged { kubeconfig: String },
}

impl K8sTarget {
    /// `--context` / `--kubeconfig` argv prefix for kubectl.
    pub fn prefix(&self) -> Vec<String> {
        match self {
            K8sTarget::Kind { context } => vec!["--context".into(), context.clone()],
            K8sTarget::SelfManaged { kubeconfig } => {
                vec!["--kubeconfig".into(), kubeconfig.clone()]
            }
        }
    }

    /// `--kube-context` / `--kubeconfig` argv prefix for helm. Helm names the
    /// context flag `--kube-context`; the bare `--context` that kubectl
    /// accepts is an unknown flag to helm, so the two tools must not share a
    /// prefix builder (issue #247: the selected target must reach every
    /// Kubernetes and Helm operation).
    pub fn helm_prefix(&self) -> Vec<String> {
        match self {
            K8sTarget::Kind { context } => vec!["--kube-context".into(), context.clone()],
            K8sTarget::SelfManaged { kubeconfig } => {
                vec!["--kubeconfig".into(), kubeconfig.clone()]
            }
        }
    }
}

/// kubectl argv = target prefix + the operation args (owned String form).
fn kubectl_args(target: &K8sTarget, args: &[&str]) -> Vec<String> {
    let mut v = target.prefix();
    v.extend(args.iter().map(|s| s.to_string()));
    v
}

/// Borrow helper matching the repo's existing `Vec<String> -> Vec<&str>` idiom.
fn to_refs(v: &[String]) -> Vec<&str> {
    v.iter().map(String::as_str).collect()
}

/// The outcome of a `kubectl get` probe, separating confirmed absence from a
/// failed query (issue #248). The deletion guard relies on positive evidence
/// that workloads are gone before the controller host may be removed, so a
/// query that fails (transport, auth, forbidden, API outage) must never read
/// as "the resource is absent".
#[derive(Debug, Clone, PartialEq, Eq)]
struct Probe {
    /// Whether the resource was confirmed present, confirmed absent, or the
    /// query failed (unknown).
    outcome: ProbeOutcome,
    /// The command's stdout. Valid only when `outcome` is `Present`; empty
    /// otherwise (for an `Absent` listing it is empty by definition, and for
    /// an `Error` it is never relied on).
    stdout: String,
}

/// The classification of a probe outcome.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProbeOutcome {
    /// The query succeeded and found the resource (a non-empty listing, or a
    /// named lookup that resolved).
    Present,
    /// The query succeeded and found nothing (an empty listing), a named
    /// lookup returned a confirmed NotFound, or the API server reported the
    /// resource type itself is not installed (nothing of that kind can
    /// exist).
    Absent,
    /// The query failed for any other reason. Absence is NOT established.
    Error,
}

/// One kubectl query's bound. The poll loops check their deadlines
/// between iterations, so a hung query (blackholed endpoint) would
/// otherwise stall teardown forever; with this bound the query fails
/// and classifies as `ProbeOutcome::Error` instead. 30s is the poll
/// cadence, so a healthy query is never cut off.
const PROBE_REQUEST_TIMEOUT: &str = "--request-timeout=30s";

/// Run a `kubectl get` probe once, classify the outcome, and return the
/// stdout so callers need not re-run the query (issue #248).
///
/// `single` tells the classifier whether the target is one named resource
/// (where a `NotFound` error is a confirmed absence) or a list (where any
/// failure is an error, never an empty listing). The command's stderr is
/// captured (not inherited) so the failure text is available for the
/// NotFound check without leaking into the user's terminal.
///
/// `request_timeout` bounds a single query (e.g. `--request-timeout=30s`).
/// The poll loops only check their deadlines between iterations, so without
/// a bound a hung query (blackholed endpoint) would stall teardown
/// indefinitely; with one, the query fails and classifies as `Error`.
async fn probe(kubectl: &str, args: &[String], single: bool, request_timeout: &str) -> Probe {
    let mut full: Vec<&str> = args.iter().map(String::as_str).collect();
    full.push(request_timeout);
    let out = Command::new(kubectl)
        .args(full)
        .output()
        .await
        .map(|o| {
            (
                o.status.success(),
                String::from_utf8_lossy(&o.stdout).into_owned(),
                String::from_utf8_lossy(&o.stderr).into_owned(),
            )
        })
        .unwrap_or_else(|e| (false, String::new(), e.to_string()));
    let (ok, stdout, stderr) = out;
    if ok {
        return if stdout.trim().is_empty() {
            Probe {
                outcome: ProbeOutcome::Absent,
                stdout: String::new(),
            }
        } else {
            Probe {
                outcome: ProbeOutcome::Present,
                stdout,
            }
        };
    }
    // Nonzero exit. A named lookup that reports NotFound, or a query the
    // API server answered with "the resource type does not exist", is a
    // confirmed absence: a reachable server positively stating the kind is
    // not installed means nothing of that kind can exist (CRDs removed by
    // an earlier teardown run). Every other failure (auth, forbidden,
    // outage) is unknown.
    let hay = format!("{stdout}\n{stderr}").to_lowercase();
    if hay.contains("the server doesn't have a resource type") {
        return Probe {
            outcome: ProbeOutcome::Absent,
            stdout: String::new(),
        };
    }
    if single && (hay.contains("not found") || hay.contains("notfound")) {
        return Probe {
            outcome: ProbeOutcome::Absent,
            stdout: String::new(),
        };
    }
    Probe {
        outcome: ProbeOutcome::Error,
        stdout: String::new(),
    }
}

/// Parse a `kubectl get clusters -A -o name` listing into the sorted,
/// deduplicated set of workload cluster names (the management cluster is
/// the host and is excluded from deletion).
fn parse_workloads(listing: &str, mgmt_cluster: &str) -> Vec<String> {
    let mut workloads: Vec<String> = listing
        .lines()
        .filter_map(|l| l.trim().rsplit('/').next())
        .filter(|n| *n != mgmt_cluster)
        .map(String::from)
        .collect();
    workloads.sort();
    workloads.dedup();
    workloads
}

/// Discover the controller host for the aws environment. Order:
/// kind cluster present (and reachable) wins (pre-pivot world); then the
/// mgmt kubeconfig (post-pivot); then unreachable.
pub async fn discover_controller_host(cfg: &Config) -> ControllerHost {
    let kind_ctx = &cfg.repo.bootstrap.kind_context;
    // `kubectl config get-contexts` lists contexts without touching a
    // cluster; kind's context existing is the pre-pivot signal.
    let contexts = capture_lossy("kubectl", &["config", "get-contexts", "-o", "name"]).await;
    if contexts.lines().any(|l| l.trim() == kind_ctx)
        && run_quiet(
            "kubectl",
            &[
                "--context",
                kind_ctx,
                "cluster-info",
                "--request-timeout=10s",
            ],
        )
        .await
    {
        return ControllerHost::Kind;
    }
    let kc = cfg.mgmt_kubeconfig.to_string_lossy().into_owned();
    if cfg.mgmt_kubeconfig.exists()
        && run_quiet(
            "kubectl",
            &kubectl_cmd(Some(&kc), &["cluster-info", "--request-timeout=10s"]),
        )
        .await
    {
        return ControllerHost::SelfManaged;
    }
    ControllerHost::Unreachable
}

/// Map a discovered controller host to the concrete Kubernetes target the
/// teardown k8s ops bind to (issue #247): kind by context name pre-pivot,
/// the mgmt kubeconfig post-pivot. Unreachable resolves to None, which the
/// caller handles as the existing early-return / orphan-sweep path.
fn resolve_target(host: &ControllerHost, cfg: &Config, tcfg: &TeardownConfig) -> Option<K8sTarget> {
    match host {
        ControllerHost::Kind => Some(K8sTarget::Kind {
            context: cfg.repo.bootstrap.kind_context.clone(),
        }),
        ControllerHost::SelfManaged => Some(K8sTarget::SelfManaged {
            kubeconfig: tcfg.mgmt_kubeconfig.to_string_lossy().into_owned(),
        }),
        ControllerHost::Unreachable => None,
    }
}

// ── The deletion guard (CLUSTERS_CONFIRMED_GONE / FORCE_KIND_DELETE) ─────────

/// Guard state for removing the controller host. The script's contract:
/// kind (now: the host) is deleted only once CAPI clusters are confirmed
/// gone, or unconditionally under FORCE_KIND_DELETE.
#[derive(Debug, PartialEq)]
pub enum HostGuard {
    /// CLUSTERS_CONFIRMED_GONE=1 equivalent: safe to remove the host.
    ConfirmedGone,
    /// FORCE_KIND_DELETE=1: remove regardless of orphan risk.
    Forced,
    /// Neither: refuse to remove the host, keep controllers running.
    Refuse,
}

impl HostGuard {
    pub fn resolve(clusters_gone: bool, force: bool) -> Self {
        if force {
            HostGuard::Forced
        } else if clusters_gone {
            HostGuard::ConfirmedGone
        } else {
            HostGuard::Refuse
        }
    }

    /// The script's refusal message, verbatim modulo the host noun.
    pub fn refusal_message(host: &str) -> String {
        format!(
            "Refusing to delete {host}: CAPI clusters were not\n\
             confirmed deleted. Leaving the CAPI controller running so AWS resources can continue\n\
             deprovisioning. Re-run teardown once 'kubectl get clusters -A' is empty,\n\
             or set FORCE_KIND_DELETE=1 to force-delete and accept orphaned AWS resources."
        )
    }
}

// ── Step helpers ──────────────────────────────────────────────────────────────

/// Step 1: suspend Flux Kustomizations on the controller host so nothing
/// recreates deleted resources mid-teardown. Patch failures are warned
/// (the script's "Could not suspend some Kustomizations – continuing
/// anyway"), not fatal.
pub async fn suspend_flux(target: &K8sTarget) -> Result<()> {
    suspend_flux_with("kubectl", target).await
}

async fn suspend_flux_with(kubectl: &str, target: &K8sTarget) -> Result<()> {
    println!(">>> Suspending Flux Kustomizations to prevent re-reconciliation...");
    let args = kubectl_args(
        target,
        &[
            "get",
            "kustomizations.kustomize.toolkit.fluxcd.io",
            "-n",
            "flux-system",
            "-o",
            "name",
        ],
    );
    // A failed listing (auth, forbidden, outage) is an unknown state, not a
    // confirmed empty one: suspending nothing would leave Flux reconciling
    // mid-teardown (issue #248).
    let p = probe(kubectl, &args, false, PROBE_REQUEST_TIMEOUT).await;
    let names = match p.outcome {
        ProbeOutcome::Absent => {
            println!("!   No Flux Kustomizations found – skipping suspension");
            return Ok(());
        }
        ProbeOutcome::Present => p.stdout,
        ProbeOutcome::Error => bail!(
 "cannot list the Flux Kustomizations to suspend them; aborting before any deletion (re-run once 'kubectl get kustomizations -n flux-system' works)"
        ),
    };
    let mut failed = 0;
    for ks in names.lines().filter_map(|l| l.trim().rsplit('/').next()) {
        let args = kubectl_args(
            target,
            &[
                "patch",
                &format!("kustomization/{ks}"),
                "-n",
                "flux-system",
                "--type",
                "merge",
                "-p",
                r#"{"spec":{"suspend":true}}"#,
            ],
        );
        if run(kubectl, &to_refs(&args)).await.is_err() {
            failed += 1;
        }
    }
    if failed > 0 {
        eprintln!("!   Could not suspend some Kustomizations – continuing anyway");
    }
    println!("✓   Flux Kustomizations suspended");
    Ok(())
}

/// Step 2+3 (local-host): delete the CAPD workload clusters and wait for
/// full deprovision (containers gone), with the script's refusal when a
/// cluster refuses to die.
pub async fn delete_capi_workloads(
    target: &K8sTarget,
    workloads: &[String],
    timeout_secs: u64,
) -> Result<()> {
    delete_capi_workloads_with("kubectl", target, workloads, timeout_secs).await
}

async fn delete_capi_workloads_with(
    kubectl: &str,
    target: &K8sTarget,
    workloads: &[String],
    timeout_secs: u64,
) -> Result<()> {
    for cluster in workloads {
        println!(">>> Deleting CAPD workload cluster '{cluster}'...");
        let args = kubectl_args(target, &["get", "cluster", cluster, "-n", "default"]);
        // The probe distinguishes confirmed absence (NotFound) from a failed
        // query (issue #248). A query error must abort, not skip: otherwise
        // an auth or API failure masquerades as "already gone" and the
        // controller host gets removed while the workload still exists.
        match probe(kubectl, &args, true, PROBE_REQUEST_TIMEOUT)
            .await
            .outcome
        {
            ProbeOutcome::Absent => {
                println!("!   Cluster '{cluster}' not found – skipping");
                continue;
            }
            ProbeOutcome::Present => {}
            ProbeOutcome::Error => bail!(
                "cannot confirm the state of CAPD workload cluster '{cluster}'; \
                 leaving the management cluster intact (re-run once \
                 'kubectl get cluster {cluster} -n default' works)"
            ),
        }
        let args = kubectl_args(
            target,
            &[
                "delete",
                "cluster",
                cluster,
                "-n",
                "default",
                "--wait=false",
            ],
        );
        run(kubectl, &to_refs(&args))
            .await
            .with_context(|| format!("failed to delete workload cluster '{cluster}'"))?;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
        loop {
            let args = kubectl_args(target, &["get", "cluster", cluster, "-n", "default"]);
            match probe(kubectl, &args, true, PROBE_REQUEST_TIMEOUT)
                .await
                .outcome
            {
                ProbeOutcome::Absent => {
                    println!("✓   CAPD workload cluster '{cluster}' deleted");
                    break;
                }
                ProbeOutcome::Present => {}
                ProbeOutcome::Error => bail!(
                    "cannot confirm the deletion of CAPD workload cluster '{cluster}'; \
                     leaving the management cluster intact (the cluster may still be \
                     deleting; re-run once the query works)"
                ),
            }
            if std::time::Instant::now() >= deadline {
                bail!(
                    "CAPD workload cluster '{cluster}' did not finish deleting within {timeout_secs}s; \
                     leaving the management cluster intact"
                );
            }
            tokio::time::sleep(std::time::Duration::from_secs(10)).await;
        }
    }
    Ok(())
}

/// Steps 2+3 (aws): delete every CAPI Cluster except the management
/// cluster itself (the host), then wait until only the mgmt remains.
/// Returns true when the workload clusters are confirmed gone.
pub async fn delete_aws_workload_clusters(
    target: &K8sTarget,
    mgmt_cluster: &str,
    timeout_secs: u64,
) -> Result<bool> {
    delete_aws_workload_clusters_with("kubectl", target, mgmt_cluster, timeout_secs).await
}

async fn delete_aws_workload_clusters_with(
    kubectl: &str,
    target: &K8sTarget,
    mgmt_cluster: &str,
    timeout_secs: u64,
) -> Result<bool> {
    println!(">>> Discovering CAPI Cluster resources...");
    let args = kubectl_args(target, &["get", "clusters", "-A", "-o", "name"]);
    // The discovery query must succeed before an empty listing may be read
    // as "no workloads" (issue #248). A failed query (auth, forbidden,
    // outage) is an unknown state, not confirmed absence, so it errors
    // instead of reporting the workloads as gone.
    let p = probe(kubectl, &args, false, PROBE_REQUEST_TIMEOUT).await;
    let workloads = match p.outcome {
        ProbeOutcome::Absent => Vec::new(),
        ProbeOutcome::Present => parse_workloads(&p.stdout, mgmt_cluster),
        ProbeOutcome::Error => bail!(
            "cannot list CAPI clusters to confirm workload deletion; \
             leaving the management cluster and CAPA controller intact \
             (re-run once 'kubectl get clusters -A' works)"
        ),
    };
    if workloads.is_empty() {
        println!(">>> No CAPI workload clusters found – skipping cluster deletion");
        return Ok(true);
    }
    for cluster in &workloads {
        println!(">>>   Deleting cluster: {cluster}");
        let args = kubectl_args(
            target,
            &["delete", "cluster", cluster, "--ignore-not-found"],
        );
        let _ = run(kubectl, &to_refs(&args)).await;
    }
    println!(">>> Waiting up to {timeout_secs}s for the workload clusters to be deleted...");
    println!(">>> (This typically takes 15–25 minutes while CAPA tears down AWS resources)");
    let start = std::time::Instant::now();
    let deadline = start + std::time::Duration::from_secs(timeout_secs);
    loop {
        let args = kubectl_args(target, &["get", "clusters", "-A", "-o", "name"]);
        // The poll must distinguish "clusters confirmed gone" (a successful
        // listing with nothing but the mgmt cluster) from a failed query.
        // An unknown state returns Ok(false) so the caller aborts and keeps
        // the management cluster intact (issue #248).
        let p = probe(kubectl, &args, false, PROBE_REQUEST_TIMEOUT).await;
        let remaining = match p.outcome {
            ProbeOutcome::Absent => Vec::new(),
            ProbeOutcome::Present => parse_workloads(&p.stdout, mgmt_cluster),
            ProbeOutcome::Error => {
                eprintln!("!   Failed to list CAPI clusters while waiting for workload deletion");
                eprintln!("!   (auth, forbidden, or API error). The workload state is UNKNOWN.");
                eprintln!(
                    "!   ABORTING teardown. The management cluster and CAPA controller have been"
                );
                eprintln!(
                    "!   left intact so AWS resources can continue to deprovision. Re-run this"
                );
                eprintln!("!   once 'kubectl get clusters -A' works (or FORCE_KIND_DELETE=1 to");
                eprintln!(
                    "!   force-delete the management cluster and accept orphaned AWS resources)."
                );
                return Ok(false);
            }
        };
        if remaining.is_empty() {
            println!("✓   All CAPI workload clusters deleted");
            return Ok(true);
        }
        let elapsed = start.elapsed().as_secs();
        if std::time::Instant::now() >= deadline {
            eprintln!("!   Timed out waiting for CAPI clusters to be deleted after {elapsed}s");
            eprintln!("!   The following clusters still exist: {remaining:?}");
            eprintln!(
                "!   ABORTING teardown. The management cluster and CAPA controller have been"
            );
            eprintln!("!   left intact so AWS resources can continue to deprovision. Re-run this");
            eprintln!("!   once 'kubectl get clusters -A' is empty (or FORCE_KIND_DELETE=1 to");
            eprintln!(
                "!   force-delete the management cluster and accept orphaned AWS resources)."
            );
            return Ok(false);
        }
        println!(
            ">>>   {} cluster(s) still deleting... ({elapsed}s elapsed, checking again in 30s)",
            remaining.len()
        );
        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
    }
}

/// Pure helper: the CAPA ownership tag key for VPC/EIP scoping.
pub fn capa_tag_key(cluster_name: &str) -> String {
    format!("sigs.k8s.io/cluster-api-provider-aws/cluster/{cluster_name}")
}

/// Pure helper: the S3 bucket name for a cluster (pattern from
/// bootstrap.toml, account from the caller).
pub fn s3_bucket_name(pattern: &str, account_id: &str, cluster_name: &str) -> String {
    pattern
        .replace("{account_id}", account_id)
        .replace("{cluster_name}", cluster_name)
}

// ── AWS orphan sweep units (best-effort: skip if absent, warn and
// continue on failure — exactly the script's semantics) ───────────────────────

fn warn(msg: &str) {
    eprintln!("!   {msg}");
}

/// `command -v` equivalent: true when the binary cannot be found or is
/// not executable on PATH (the script's preflight probe).
async fn which_failure(cmd: &str) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let Some(path) = std::env::var_os("PATH") else {
            return true;
        };
        !std::env::split_paths(&path).any(|dir| {
            let candidate = dir.join(cmd);
            candidate
                .metadata()
                .map(|m| m.permissions().mode() & 0o111 != 0)
                .unwrap_or(false)
        })
    }
    #[cfg(not(unix))]
    {
        !run_quiet(cmd, &["--version"]).await
    }
}

/// One AWS workload (or the management cluster itself) expressed as the
/// names the sweep needs, derived from the config.
#[derive(Clone, Debug)]
pub struct AwsSweepTarget {
    pub region: String,
    pub cluster_name: String,
    pub eks_cluster_name: String,
    pub rds_instance: String,
}

impl AwsSweepTarget {
    pub fn from_workload(w: &crate::config::AwsWorkload) -> Self {
        Self {
            region: w.region.clone(),
            cluster_name: w.cluster_name.clone(),
            eks_cluster_name: w.eks_cluster_name.clone(),
            rds_instance: w.rds_instance.clone(),
        }
    }

    pub fn mgmt(region: &str, cluster_name: &str, eks_cluster_name: &str) -> Self {
        Self {
            region: region.to_string(),
            cluster_name: cluster_name.to_string(),
            eks_cluster_name: eks_cluster_name.to_string(),
            // The management cluster runs no workload ACK controllers,
            // so it owns no RDS instance; the sweep unit skips absent ids.
            rds_instance: format!("krops-{cluster_name}-db"),
        }
    }

    pub fn bucket_name(&self, pattern: &str, account_id: &str) -> String {
        s3_bucket_name(pattern, account_id, &self.cluster_name)
    }

    pub fn capa_tag_key(&self) -> String {
        capa_tag_key(&self.cluster_name)
    }
}

fn aws_base(region: &str) -> Vec<String> {
    vec!["--region".into(), region.into()]
}

/// True when the EKS cluster exists (used by every EKS-gated unit).
async fn eks_exists(cluster: &str, region: &str) -> bool {
    run_quiet(
        "aws",
        &[
            "eks",
            "describe-cluster",
            "--name",
            cluster,
            "--region",
            region,
            "--output",
            "json",
        ],
    )
    .await
}

/// 4a. Pod identity associations (only possible while EKS exists).
pub async fn cleanup_pod_identity_associations(target: &AwsSweepTarget) {
    if !eks_exists(&target.eks_cluster_name, &target.region).await {
        println!(
            "✓   EKS cluster {} not found in {} – no pod identity associations to clean",
            target.eks_cluster_name, target.region
        );
        return;
    }
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "eks".into(),
        "list-pod-identity-associations".into(),
        "--cluster-name".into(),
        target.eks_cluster_name.clone(),
        "--query".into(),
        "associations[].associationId".into(),
        "--output".into(),
        "text".into(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    let ids = capture_lossy("aws", &argrefs).await;
    for id in ids.split_whitespace() {
        println!(">>>   Deleting pod identity association: {id}");
        let mut args = aws_base(&target.region);
        args.extend_from_slice(&[
            "eks".into(),
            "delete-pod-identity-association".into(),
            "--cluster-name".into(),
            target.eks_cluster_name.clone(),
            "--association-id".into(),
            id.into(),
        ]);
        let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
        if !run_quiet("aws", &argrefs).await {
            warn(&format!("Failed to delete pod identity association {id}"));
        }
    }
}

/// 4b. Nodegroups: delete in every region first, then wait (EKS refuses
/// cluster deletion while nodegroups exist).
pub async fn cleanup_nodegroups(target: &AwsSweepTarget) {
    if !eks_exists(&target.eks_cluster_name, &target.region).await {
        return;
    }
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "eks".into(),
        "list-nodegroups".into(),
        "--cluster-name".into(),
        target.eks_cluster_name.clone(),
        "--query".into(),
        "nodegroups[]".into(),
        "--output".into(),
        "text".into(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    let ngs = capture_lossy("aws", &argrefs).await;
    for ng in ngs.split_whitespace() {
        println!(">>>   Deleting nodegroup: {ng}");
        let mut args = aws_base(&target.region);
        args.extend_from_slice(&[
            "eks".into(),
            "delete-nodegroup".into(),
            "--cluster-name".into(),
            target.eks_cluster_name.clone(),
            "--nodegroup-name".into(),
            ng.into(),
        ]);
        let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
        if !run_quiet("aws", &argrefs).await {
            warn(&format!("Failed to delete nodegroup {ng}"));
        }
    }
}

pub async fn wait_nodegroups_deleted(target: &AwsSweepTarget) {
    if !eks_exists(&target.eks_cluster_name, &target.region).await {
        return;
    }
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "eks".into(),
        "list-nodegroups".into(),
        "--cluster-name".into(),
        target.eks_cluster_name.clone(),
        "--query".into(),
        "nodegroups[]".into(),
        "--output".into(),
        "text".into(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    let ngs = capture_lossy("aws", &argrefs).await;
    for ng in ngs.split_whitespace() {
        println!(">>>   Waiting for nodegroup {ng} to finish deleting...");
        let mut args = aws_base(&target.region);
        args.extend_from_slice(&[
            "eks".into(),
            "wait".into(),
            "nodegroup-deleted".into(),
            "--cluster-name".into(),
            target.eks_cluster_name.clone(),
            "--nodegroup-name".into(),
            ng.into(),
        ]);
        let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
        if !run_quiet("aws", &argrefs).await {
            warn(&format!("Timed out waiting for nodegroup {ng}"));
        }
    }
}

/// 4c. EKS cluster deletion + wait (control-plane ENIs block VPC cleanup).
pub async fn cleanup_eks_cluster(target: &AwsSweepTarget) {
    if !eks_exists(&target.eks_cluster_name, &target.region).await {
        println!(
            "✓   EKS cluster {} not found in {}",
            target.eks_cluster_name, target.region
        );
        return;
    }
    println!(
        ">>>   Deleting EKS cluster: {} in {}",
        target.eks_cluster_name, target.region
    );
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "eks".into(),
        "delete-cluster".into(),
        "--name".into(),
        target.eks_cluster_name.clone(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    if !run_quiet("aws", &argrefs).await {
        warn(&format!(
            "Failed to delete EKS cluster {}",
            target.eks_cluster_name
        ));
    }
}

pub async fn wait_eks_cluster_deleted(target: &AwsSweepTarget) {
    if !eks_exists(&target.eks_cluster_name, &target.region).await {
        return;
    }
    println!(
        ">>>   Waiting for EKS cluster {} to finish deleting...",
        target.eks_cluster_name
    );
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "eks".into(),
        "wait".into(),
        "cluster-deleted".into(),
        "--name".into(),
        target.eks_cluster_name.clone(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    if !run_quiet("aws", &argrefs).await {
        warn(&format!(
            "Timed out waiting for EKS cluster {}",
            target.eks_cluster_name
        ));
    }
}

/// 4d. RDS instances orphaned when their workload cluster died first.
pub async fn cleanup_rds_instance(target: &AwsSweepTarget) {
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "rds".into(),
        "describe-db-instances".into(),
        "--db-instance-identifier".into(),
        target.rds_instance.clone(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    if !run_quiet("aws", &argrefs).await {
        println!(
            "✓   RDS instance {} not found in {}",
            target.rds_instance, target.region
        );
        return;
    }
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "rds".into(),
        "describe-db-instances".into(),
        "--db-instance-identifier".into(),
        target.rds_instance.clone(),
        "--query".into(),
        "DBInstances[0].DBInstanceStatus".into(),
        "--output".into(),
        "text".into(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    let status = capture_lossy("aws", &argrefs).await.trim().to_string();
    if status == "deleting" {
        println!(
            ">>>   RDS instance {} already deleting in {}",
            target.rds_instance, target.region
        );
        return;
    }
    println!(
        ">>>   Deleting RDS instance: {} in {}",
        target.rds_instance, target.region
    );
    let mut args = aws_base(&target.region);
    args.extend_from_slice(&[
        "rds".into(),
        "delete-db-instance".into(),
        "--db-instance-identifier".into(),
        target.rds_instance.clone(),
        "--skip-final-snapshot".into(),
        "--delete-automated-backups".into(),
    ]);
    let argrefs: Vec<&str> = args.iter().map(String::as_str).collect();
    if !run_quiet("aws", &argrefs).await {
        warn(&format!(
            "Failed to delete RDS instance {}",
            target.rds_instance
        ));
    }
}

/// 4f. S3 buckets: versioned, so every object version AND delete marker
/// must be purged before the bucket itself can be deleted.
pub async fn cleanup_s3_bucket(bucket: &str, region: &str) {
    let head = run_quiet(
        "aws",
        &[
            "s3api",
            "head-bucket",
            "--bucket",
            bucket,
            "--region",
            region,
        ],
    )
    .await;
    if !head {
        println!("✓   S3 bucket {bucket} not found");
        return;
    }
    println!(">>>   Emptying S3 bucket: {bucket} (all versions and delete markers)");
    loop {
        let batch = capture_lossy(
            "aws",
            &[
                "s3api",
                "list-object-versions",
                "--bucket",
                bucket,
                "--region",
                region,
                "--max-items",
                "500",
                "--query",
                "{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}, Quiet: `true`}",
                "--output",
                "json",
            ],
        ).await;
        if !batch.contains("\"Key\"") {
            break;
        }
        let ok = run_with_stdin_str(
            "aws",
            &[
                "s3api",
                "delete-objects",
                "--bucket",
                bucket,
                "--region",
                region,
                "--delete",
                "fileb:///dev/stdin",
            ],
            &batch,
        )
        .await;
        if !ok {
            warn(&format!("Failed to purge objects from {bucket}"));
            break;
        }
    }
    println!(">>>   Deleting S3 bucket: {bucket}");
    if !run_quiet(
        "aws",
        &[
            "s3api",
            "delete-bucket",
            "--bucket",
            bucket,
            "--region",
            region,
        ],
    )
    .await
    {
        warn(&format!("Failed to delete S3 bucket {bucket}"));
    }
}

/// 4g. IAM role (detach policies, instance profiles, inline policies,
/// then the role). Skips silently when the role is absent.
pub async fn cleanup_iam_role(role: &str) {
    if !run_quiet("aws", &["iam", "get-role", "--role-name", role]).await {
        return;
    }
    println!(">>>   Deleting IAM role: {role}");
    let policies = capture_lossy(
        "aws",
        &[
            "iam",
            "list-attached-role-policies",
            "--role-name",
            role,
            "--query",
            "AttachedPolicies[].PolicyArn",
            "--output",
            "text",
        ],
    )
    .await;
    for arn in policies.split_whitespace() {
        println!(">>>     Detaching policy: {arn}");
        let _ = run_quiet(
            "aws",
            &[
                "iam",
                "detach-role-policy",
                "--role-name",
                role,
                "--policy-arn",
                arn,
            ],
        )
        .await;
    }
    let profiles = capture_lossy(
        "aws",
        &[
            "iam",
            "list-instance-profiles-for-role",
            "--role-name",
            role,
            "--query",
            "InstanceProfiles[].InstanceProfileName",
            "--output",
            "text",
        ],
    )
    .await;
    for profile in profiles.split_whitespace() {
        println!(">>>     Deleting instance profile: {profile}");
        let _ = run_quiet(
            "aws",
            &[
                "iam",
                "remove-role-from-instance-profile",
                "--instance-profile-name",
                profile,
                "--role-name",
                role,
            ],
        )
        .await;
        let _ = run_quiet(
            "aws",
            &[
                "iam",
                "delete-instance-profile",
                "--instance-profile-name",
                profile,
            ],
        )
        .await;
    }
    let inline = capture_lossy(
        "aws",
        &[
            "iam",
            "list-role-policies",
            "--role-name",
            role,
            "--query",
            "PolicyNames[]",
            "--output",
            "text",
        ],
    )
    .await;
    for policy in inline.split_whitespace() {
        println!(">>>     Deleting inline policy: {policy}");
        let _ = run_quiet(
            "aws",
            &[
                "iam",
                "delete-role-policy",
                "--role-name",
                role,
                "--policy-name",
                policy,
            ],
        )
        .await;
    }
    if !run_quiet("aws", &["iam", "delete-role", "--role-name", role]).await {
        warn(&format!("Failed to delete IAM role {role}"));
    }
}

/// CAPA (EKSEnableIAM) auto-creates per-cluster roles not declared in
/// Git; sweep every role whose name starts with the cluster name.
pub async fn cleanup_capa_iam_roles(prefix: &str) {
    // list-roles is paginated at 100 by the CLI without a paginator on
    // server-side filters; use the query prefix sweep like the script.
    let roles = capture_lossy(
        "aws",
        &[
            "iam",
            "list-roles",
            "--query",
            &format!("Roles[?starts_with(RoleName, `{prefix}`)].RoleName"),
            "--output",
            "text",
            "--max-items",
            "1000",
        ],
    )
    .await;
    for role in roles.split_whitespace() {
        if role == "None" {
            continue;
        }
        cleanup_iam_role(role).await;
    }
}

/// 4g. IAM user (login profile, access keys, inline policies, user).
pub async fn cleanup_iam_user(user: &str) {
    if !run_quiet("aws", &["iam", "get-user", "--user-name", user]).await {
        return;
    }
    println!(">>>   Deleting IAM user: {user}");
    let _ = run_quiet("aws", &["iam", "delete-login-profile", "--user-name", user]).await;
    let keys = capture_lossy(
        "aws",
        &[
            "iam",
            "list-access-keys",
            "--user-name",
            user,
            "--query",
            "AccessKeyMetadata[].AccessKeyId",
            "--output",
            "text",
        ],
    )
    .await;
    for key in keys.split_whitespace() {
        println!(">>>     Deleting access key: {key}");
        let _ = run_quiet(
            "aws",
            &[
                "iam",
                "delete-access-key",
                "--user-name",
                user,
                "--access-key-id",
                key,
            ],
        )
        .await;
    }
    let inline = capture_lossy(
        "aws",
        &[
            "iam",
            "list-user-policies",
            "--user-name",
            user,
            "--query",
            "PolicyNames[]",
            "--output",
            "text",
        ],
    )
    .await;
    for policy in inline.split_whitespace() {
        let _ = run_quiet(
            "aws",
            &[
                "iam",
                "delete-user-policy",
                "--user-name",
                user,
                "--policy-name",
                policy,
            ],
        )
        .await;
    }
    if !run_quiet("aws", &["iam", "delete-user", "--user-name", user]).await {
        warn(&format!("Failed to delete IAM user {user}"));
    }
}

/// 4h. CloudFormation bootstrap stack.
pub async fn cleanup_cfn_stack(stack: &str, region: &str) {
    if !run_quiet(
        "aws",
        &[
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack,
            "--region",
            region,
        ],
    )
    .await
    {
        return;
    }
    println!(">>>   Deleting CFN stack: {stack} in {region}");
    if !run_quiet(
        "aws",
        &[
            "cloudformation",
            "delete-stack",
            "--stack-name",
            stack,
            "--region",
            region,
        ],
    )
    .await
    {
        warn(&format!("Failed to delete CFN stack {stack} in {region}"));
    }
}

/// 4e. VPC resources, gated on the CAPA ownership tag (krops scope
/// only): NAT gateways + their EIPs, subnets, IGWs, route tables,
/// security groups (rules first, then the groups), the VPC itself.
pub async fn cleanup_vpc_resources(target: &AwsSweepTarget) {
    let tag = target.capa_tag_key();
    let vpcs = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-vpcs",
            "--region",
            &target.region,
            "--filters",
            &format!("Name=tag:{tag},Values=owned"),
            "--query",
            "Vpcs[].VpcId",
            "--output",
            "text",
        ],
    )
    .await;
    for vpc in vpcs.split_whitespace() {
        println!(
            ">>>   Cleaning up VPC {vpc} in {} (cluster: {})",
            target.region, target.cluster_name
        );
        cleanup_vpc(vpc, &target.region, &tag).await;
    }
}

async fn cleanup_vpc(vpc: &str, region: &str, tag: &str) {
    // NAT gateways (before subnets), including the deleting state.
    let nats = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-nat-gateways",
            "--region",
            region,
            "--filter",
            &format!("Name=vpc-id,Values={vpc}"),
            "Name=state,Values=pending,available,deleting",
            "--query",
            "NatGateways[].NatGatewayId",
            "--output",
            "text",
        ],
    )
    .await;
    for nat in nats.split_whitespace() {
        println!(">>>     Deleting NAT gateway: {nat}");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "delete-nat-gateway",
                "--region",
                region,
                "--nat-gateway-id",
                nat,
            ],
        )
        .await;
    }
    for nat in nats.split_whitespace() {
        println!(">>>     Waiting for NAT gateway {nat} to finish deleting...");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "wait",
                "nat-gateway-deleted",
                "--region",
                region,
                "--nat-gateway-ids",
                nat,
            ],
        )
        .await;
    }
    // Elastic IPs (tagged, NAT-allocated ones are not released with it).
    let eips = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-addresses",
            "--region",
            region,
            "--filters",
            &format!("Name=tag:{tag},Values=owned"),
            "--query",
            "Addresses[].AllocationId",
            "--output",
            "text",
        ],
    )
    .await;
    for eip in eips.split_whitespace() {
        println!(">>>     Releasing Elastic IP: {eip}");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "release-address",
                "--region",
                region,
                "--allocation-id",
                eip,
            ],
        )
        .await;
    }
    // Subnets (the VPC is CAPA-tagged, never the default VPC).
    let subnets = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-subnets",
            "--region",
            region,
            "--filters",
            &format!("Name=vpc-id,Values={vpc}"),
            "--query",
            "Subnets[].SubnetId",
            "--output",
            "text",
        ],
    )
    .await;
    for subnet in subnets.split_whitespace() {
        println!(">>>     Deleting subnet: {subnet}");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "delete-subnet",
                "--region",
                region,
                "--subnet-id",
                subnet,
            ],
        )
        .await;
    }
    // Internet gateways: detach, then delete.
    let igws = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-internet-gateways",
            "--region",
            region,
            "--filters",
            &format!("Name=attachment.vpc-id,Values={vpc}"),
            "--query",
            "InternetGateways[].InternetGatewayId",
            "--output",
            "text",
        ],
    )
    .await;
    for igw in igws.split_whitespace() {
        println!(">>>     Detaching internet gateway: {igw}");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "detach-internet-gateway",
                "--region",
                region,
                "--internet-gateway-id",
                igw,
                "--vpc-id",
                vpc,
            ],
        )
        .await;
        println!(">>>     Deleting internet gateway: {igw}");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "delete-internet-gateway",
                "--region",
                region,
                "--internet-gateway-id",
                igw,
            ],
        )
        .await;
    }
    // Route tables (skip the main).
    let main_rt = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-route-tables",
            "--region",
            region,
            "--filters",
            &format!("Name=vpc-id,Values={vpc}"),
            "Name=main,Values=true",
            "--query",
            "RouteTables[0].RouteTableId",
            "--output",
            "text",
        ],
    )
    .await
    .trim()
    .to_string();
    let rts = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-route-tables",
            "--region",
            region,
            "--filters",
            &format!("Name=vpc-id,Values={vpc}"),
            "--query",
            "RouteTables[].RouteTableId",
            "--output",
            "text",
        ],
    )
    .await;
    for rt in rts.split_whitespace() {
        if rt == main_rt {
            continue;
        }
        println!(">>>     Deleting route table: {rt}");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "delete-route-table",
                "--region",
                region,
                "--route-table-id",
                rt,
            ],
        )
        .await;
    }
    // Security groups reference each other: strip all rules, then delete.
    let sgs = capture_lossy(
        "aws",
        &[
            "ec2",
            "describe-security-groups",
            "--region",
            region,
            "--filters",
            &format!("Name=vpc-id,Values={vpc}"),
            "--query",
            "SecurityGroups[?GroupName!=`default`].GroupId",
            "--output",
            "text",
        ],
    )
    .await;
    let sg_list: Vec<String> = sgs.split_whitespace().map(String::from).collect();
    for sg in &sg_list {
        let ingress = capture_lossy(
            "aws",
            &[
                "ec2",
                "describe-security-groups",
                "--region",
                region,
                "--group-ids",
                sg,
                "--query",
                "SecurityGroups[0].IpPermissions",
                "--output",
                "json",
            ],
        )
        .await;
        if ingress.trim() != "[]" && !ingress.trim().is_empty() {
            let _ = run_quiet(
                "aws",
                &[
                    "ec2",
                    "revoke-security-group-ingress",
                    "--region",
                    region,
                    "--group-id",
                    sg,
                    "--ip-permissions",
                    ingress.trim(),
                ],
            )
            .await;
        }
        let egress = capture_lossy(
            "aws",
            &[
                "ec2",
                "describe-security-groups",
                "--region",
                region,
                "--group-ids",
                sg,
                "--query",
                "SecurityGroups[0].IpPermissionsEgress",
                "--output",
                "json",
            ],
        )
        .await;
        if egress.trim() != "[]" && !egress.trim().is_empty() {
            let _ = run_quiet(
                "aws",
                &[
                    "ec2",
                    "revoke-security-group-egress",
                    "--region",
                    region,
                    "--group-id",
                    sg,
                    "--ip-permissions",
                    egress.trim(),
                ],
            )
            .await;
        }
    }
    for sg in &sg_list {
        println!(">>>     Deleting security group: {sg}");
        let _ = run_quiet(
            "aws",
            &[
                "ec2",
                "delete-security-group",
                "--region",
                region,
                "--group-id",
                sg,
            ],
        )
        .await;
    }
    println!(">>>     Deleting VPC: {vpc}");
    if !run_quiet(
        "aws",
        &["ec2", "delete-vpc", "--region", region, "--vpc-id", vpc],
    )
    .await
    {
        warn(&format!("Failed to delete VPC {vpc}"));
    }
}

/// run_with_stdin, but best-effort (returns success instead of raising).
async fn run_with_stdin_str(cmd: &str, args: &[&str], input: &str) -> bool {
    use tokio::io::AsyncWriteExt;
    let mut child = match Command::new(cmd).args(args).stdin(Stdio::piped()).spawn() {
        Ok(c) => c,
        Err(_) => return false,
    };
    if let Some(mut stdin) = child.stdin.take() {
        if stdin.write_all(input.as_bytes()).await.is_err() {
            return false;
        }
    }
    matches!(child.wait().await, Ok(s) if s.success())
}

// ── Orchestrator: the nine steps in the script's order ───────────────────────

/// kubectl jsonpath printing `ns/name` per line. Byte-exact matters:
/// kubectl rejects an over-escaped form with "unrecognized character in
/// action: U+005C" and capture_lossy would turn that into an empty
/// listing. The unit test pins the exact argv bytes; the live form was
/// verified against a kind cluster (prints ns/name per line).
const NS_NAME_JSONPATH: &str =
    "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{\"\\n\"}{end}";

/// Step 5: delete CAPI provider CRs (the operator uninstalls the
/// controllers), then wait up to the timeout. A failed provider query is an
/// unknown state, not "no providers" (issue #248), so the function reports
/// the failure and the caller aborts instead of claiming the controllers are
/// gone.
pub async fn delete_capi_providers(target: &K8sTarget, timeout_secs: u64) -> Result<()> {
    delete_capi_providers_with("kubectl", target, timeout_secs).await
}

async fn delete_capi_providers_with(
    kubectl: &str,
    target: &K8sTarget,
    timeout_secs: u64,
) -> Result<()> {
    println!(">>> Deleting CAPI providers...");
    let kinds = [
        "addonproviders",
        "controlplaneproviders",
        "bootstrapproviders",
        "infrastructureproviders",
        "coreproviders",
    ];
    let mut deleted_any = false;
    for kind in kinds {
        let full = format!("{kind}.operator.cluster.x-k8s.io");
        // The script's jsonpath: `ns/name` per line (-o name never
        // includes the namespace for cluster-scoped listings).
        let args = kubectl_args(target, &["get", &full, "-A", "-o", NS_NAME_JSONPATH]);
        // A failed listing must not read as "no providers of this kind"
        // (issue #248).
        let p = probe(kubectl, &args, false, PROBE_REQUEST_TIMEOUT).await;
        let listing = match p.outcome {
            ProbeOutcome::Absent => String::new(),
            ProbeOutcome::Present => p.stdout,
            ProbeOutcome::Error => bail!(
                "cannot list the CAPI {kind} providers to confirm their state; aborting (re-run once 'kubectl get {full} -A' works)"
            ),
        };
        for object in listing.lines().filter_map(|l| {
            let l = l.trim();
            if l.is_empty() {
                None
            } else {
                Some(l)
            }
        }) {
            let (ns, name) = match object.split_once('/') {
                Some((ns, name)) => (ns, name),
                None => ("default", object),
            };
            println!(">>>   Deleting {kind}: {name} (namespace: {ns})");
            let args = kubectl_args(
                target,
                &["delete", &full, name, "-n", ns, "--ignore-not-found"],
            );
            let _ = run(kubectl, &to_refs(&args)).await;
            deleted_any = true;
        }
    }
    if !deleted_any {
        println!("!   CAPI Operator CRDs not present – skipping provider deletion");
        return Ok(());
    }
    println!(">>> Waiting up to {timeout_secs}s for CAPI providers to be removed...");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    loop {
        let mut remaining = 0;
        for kind in kinds {
            let full = format!("{kind}.operator.cluster.x-k8s.io");
            let args = kubectl_args(target, &["get", &full, "-A", "--no-headers"]);
            // A failed poll must not count as zero providers remaining
            // (issue #248).
            let p = probe(kubectl, &args, false, PROBE_REQUEST_TIMEOUT).await;
            match p.outcome {
                ProbeOutcome::Absent => {}
                ProbeOutcome::Present => {
                    remaining += p.stdout.lines().filter(|l| !l.trim().is_empty()).count()
                }
                ProbeOutcome::Error => bail!(
                    "cannot confirm the removal of the CAPI providers; aborting (re-run once 'kubectl get {full} -A' works)"
                ),
            }
        }
        if remaining == 0 {
            println!("✓   All CAPI providers removed");
            return Ok(());
        }
        if std::time::Instant::now() >= deadline {
            warn("Timed out waiting for CAPI providers to be removed – continuing anyway");
            return Ok(());
        }
        println!(">>>   {remaining} provider(s) still removing...");
        tokio::time::sleep(std::time::Duration::from_secs(15)).await;
    }
}

/// Steps 6-7: uninstall the bootstrap Helm releases (flux, then
/// flux-operator). Factored out of `uninstall_helm_and_secrets` so the target
/// binding (helm's `--kube-context` for kind, `--kubeconfig` for self-managed)
/// is testable with a stub helm (issue #247).
async fn uninstall_helm_with(helm: &str, releases: &[&str], namespace: &str, target: &K8sTarget) {
    for release in releases {
        println!(">>> Uninstalling {release} Helm release...");
        let prefix = target.helm_prefix();
        let kc: Vec<&str> = to_refs(&prefix);
        let status = {
            let mut args = kc.clone();
            args.extend_from_slice(&["status", release, "-n", namespace]);
            capture_lossy(helm, &args).await
        };
        if status.trim().is_empty() || status.contains("not found") || status.contains("Error") {
            println!("!   {release} Helm release not found – skipping");
            continue;
        }
        let mut args = kc.clone();
        args.extend_from_slice(&[
            "uninstall",
            release,
            "--namespace",
            namespace,
            "--wait",
            "--timeout",
            "5m0s",
        ]);
        if run(helm, &args).await.is_ok() {
            println!("✓   {release} Helm release uninstalled");
        } else {
            warn(&format!("{release} Helm release could not be uninstalled"));
        }
    }
}

/// Steps 6-8: helm releases then secrets (aws path, target threaded).
pub async fn uninstall_helm_and_secrets(cfg: &Config, target: &K8sTarget) {
    uninstall_helm_with("helm", &["flux", "flux-operator"], "flux-system", target).await;

    println!(">>> Deleting GitHub PAT and SOPS age secrets...");
    let ns = &cfg.repo.bootstrap.flux_namespace;
    let pat = &cfg.repo.bootstrap.github_pat_secret;
    let age = &cfg.repo.bootstrap.sops_age_secret;
    for secret in [pat.as_str(), age.as_str(), "aws-credentials"] {
        let namespace = if secret == "aws-credentials" {
            "capa-system"
        } else {
            ns
        };
        let args = kubectl_args(
            target,
            &[
                "delete",
                "secret",
                secret,
                "-n",
                namespace,
                "--ignore-not-found",
            ],
        );
        let _ = run("kubectl", &to_refs(&args)).await;
    }
    println!("✓   Secrets deleted (or were already absent)");
}

/// local-host: remove the self-managed mgmt's containers at the engine
/// level (validated live 2026-09-01; a cluster cannot delete its own
/// Cluster object cleanly).
pub async fn remove_local_mgmt_containers(engine: &str, prefix: &str) {
    let listing = capture_lossy(engine, &["ps", "-a", "--format", "{{.Names}}"]).await;
    let targets: Vec<&str> = listing
        .lines()
        .map(str::trim)
        .filter(|n| n.starts_with(prefix))
        .collect();
    if targets.is_empty() {
        println!(">>> No '{prefix}*' containers found – management cluster already gone");
        return;
    }
    let target_refs: Vec<&str> = targets.clone();
    println!(
        ">>> Removing self-managed management cluster containers: {}",
        target_refs.join(", ")
    );
    let mut args = vec!["rm", "-f"];
    args.extend(target_refs);
    if run(engine, &args).await.is_ok() {
        println!("✓   management cluster containers removed");
    } else {
        warn("some management cluster containers could not be removed");
    }
}

/// local-host registry removal (best-effort, engine may be down).
pub async fn remove_registry_container(engine: Option<&str>, name: &str) {
    let Some(engine) = engine else {
        println!("!   Container engine unavailable; registry cleanup skipped");
        return;
    };
    let listing = capture_lossy(
        engine,
        &[
            "ps",
            "-a",
            "--filter",
            &format!("name=^{name}$"),
            "--format",
            "{{.Names}}",
        ],
    )
    .await;
    if !listing.lines().any(|l| l.trim() == name) {
        return;
    }
    println!(">>> Removing local registry container '{name}'...");
    if run(engine, &["rm", "-f", name]).await.is_ok() {
        println!("✓   registry container '{name}' removed");
    } else {
        warn("registry container could not be removed");
    }
}

/// Environments whose teardown is not automated declare
/// `[environments.<name>.teardown] manual = "..."` (issue #71); the run
/// refuses before any preflight or mutation and prints the text.
pub fn manual_teardown_refusal(env_name: &str, td: &crate::config::TeardownEnv) -> Option<String> {
    td.manual
        .as_ref()
        .map(|text| format!("teardown is manual for the '{env_name}' environment:\n{text}"))
}

/// The full teardown run. Mirrors teardown.sh's flow with the
/// post-pivot controller-host resolution.
#[allow(clippy::too_many_arguments)]
pub async fn run_teardown(cfg: &Config, tcfg: &TeardownConfig) -> Result<()> {
    let env = &cfg.environment;
    let td = &env.teardown;
    if let Some(msg) = manual_teardown_refusal(&cfg.profile, td) {
        bail!("{msg}");
    }

    // Never let the AWS CLI open an interactive pager.
    std::env::set_var("AWS_PAGER", "");

    // ── Preflight: tools and mode rules ───────────────────────────────
    // The script's tool preflight, restored: hard-fail on a missing tool
    // BEFORE any mutation. A PATH problem must never downgrade the aws
    // path into a blind AWS-only sweep against a live world.
    let aws_available = !which_failure("aws").await;
    if tcfg.aws_only {
        if !aws_available {
            bail!("aws CLI not found in PATH (required for AWS_ONLY mode)");
        }
        println!(">>> AWS_ONLY mode – k8s tools not required");
    } else if cfg.is_local() {
        // The script probes existence (command -v) for both tools; probing
        // execution instead breaks on kubectl, which rejects --version as
        // an unknown flag and would fail preflight on every host.
        for cmd in ["kind", "kubectl"] {
            if which_failure(cmd).await {
                bail!("{cmd} not found in PATH");
            }
        }
        if !aws_available {
            eprintln!("!   aws CLI not found – AWS orphan cleanup (step 4) will be skipped");
        }
    } else {
        for cmd in ["kind", "helm", "kubectl", "xargs"] {
            if which_failure(cmd).await {
                bail!("{cmd} not found in PATH");
            }
        }
        if aws_available {
            println!(">>> aws CLI available");
        } else {
            eprintln!("!   aws CLI not found – AWS orphan cleanup (step 4) will be skipped");
        }
    }

    // local-host path.
    if cfg.is_local() {
        if tcfg.aws_only {
            bail!("AWS_ONLY=1 cannot be combined with the local-host profile\n       Use the AWS profile for AWS-only orphan cleanup");
        }
        let engine = detect_engine().await;
        let kind_present = run_quiet("kind", &["get", "clusters"]).await
            && capture_lossy("kind", &["get", "clusters"])
                .await
                .lines()
                .any(|l| l.trim() == cfg.repo.bootstrap.kind_cluster);

        let kc: Option<String> = if kind_present {
            // Toolbox runs: join the kind network so the internal API
            // endpoint resolves, then rewrite KUBECONFIG to kind's
            // internal kubeconfig. kubectl picks KUBECONFIG up from the
            // environment, so the k8s calls below need no --kubeconfig.
            if cfg.toolbox {
                if let Some(engine) = engine.as_deref() {
                    toolbox_join_kind_network(cfg, engine).await?;
                    select_toolbox_kind_kubeconfig(cfg).await?;
                }
            }
            let _ = run(
                "kubectl",
                &["config", "use-context", &cfg.repo.bootstrap.kind_context],
            )
            .await;
            None
        } else {
            // Post-pivot: the self-managed mgmt kubeconfig.
            let kc_path = tcfg.mgmt_kubeconfig.to_string_lossy().into_owned();
            if tcfg.mgmt_kubeconfig.exists() {
                Some(kc_path)
            } else {
                None
            }
        };

        // The krops kubectl calls bind to an explicit target (issue #247):
        // the kind bootstrap context pre-pivot, the mgmt kubeconfig
        // post-pivot - never the caller's current context.
        let target: Option<K8sTarget> = if kind_present {
            Some(K8sTarget::Kind {
                context: cfg.repo.bootstrap.kind_context.clone(),
            })
        } else {
            kc.as_ref().map(|k| K8sTarget::SelfManaged {
                kubeconfig: k.clone(),
            })
        };

        // Suspend the workload Kustomization (prevents Flux recreating
        // the Cluster while CAPD removes its machines), then delete the
        // workload clusters, then remove the controller host.
        if let Some(kc) = kc.as_deref() {
            let _ = run(
                "kubectl",
                &kubectl_cmd(
                    Some(kc),
                    &[
                        "patch",
                        "kustomization/docker-workload-cluster",
                        "-n",
                        "flux-system",
                        "--type",
                        "merge",
                        "-p",
                        r#"{"spec":{"suspend":true}}"#,
                    ],
                ),
            )
            .await;
            if let Some(target) = &target {
                delete_capi_workloads(target, &td.capi_workloads, 300).await?;
            }
        } else if let Some(target) = &target {
            delete_capi_workloads(target, &td.capi_workloads, 300).await?;
        } else {
            println!(">>> No reachable management cluster; skipping CAPI workload deletion");
        }

        // Remove the controller host under the guard.
        if kind_present {
            // Workload clusters confirmed gone (or the deletion above
            // would have refused); delete kind.
            println!(
                ">>> Deleting kind management cluster '{}'...",
                cfg.repo.bootstrap.kind_cluster
            );
            // Toolbox runs must leave the kind network first: kind removes
            // the network with the last node, and an attached toolbox
            // container would keep it alive.
            if cfg.toolbox {
                if let Some(engine) = engine.as_deref() {
                    toolbox_leave_kind_network(cfg, engine).await;
                }
            }
            let kind_name = cfg.repo.bootstrap.kind_cluster.clone();
            if run("kind", &["delete", "cluster", "--name", &kind_name])
                .await
                .is_ok()
            {
                println!("✓   kind cluster '{kind_name}' deleted");
            } else {
                warn("kind cluster could not be deleted – it may already be gone");
            }
        } else if let Some(prefix) = td.mgmt_container_prefix.as_deref() {
            if let Some(engine) = engine.as_deref() {
                remove_local_mgmt_containers(engine, prefix).await;
            } else {
                println!("!   Container engine unavailable; management container cleanup skipped");
            }
        }

        if let Some(engine) = engine.as_deref() {
            remove_registry_container(Some(engine), &cfg.repo.bootstrap.registry_name).await;
        } else {
            remove_registry_container(None, &cfg.repo.bootstrap.registry_name).await;
        }
        println!();
        println!("✓ Teardown complete.");
        return Ok(());
    }

    // ── local-talos path (bare metal; issue #105 scope item 8) ────────
    if td.hardware_release {
        if tcfg.aws_only {
            bail!(
                "AWS_ONLY=1 cannot be combined with the local-talos profile\n       There is no AWS orphan sweep for operator-owned hardware"
            );
        }
        for cmd in ["kind", "kubectl"] {
            if which_failure(cmd).await {
                bail!("{cmd} not found in PATH");
            }
        }

        // The CAPI inventory lives in kind pre-pivot, in the management
        // cluster itself post-pivot (same discovery as aws).
        let host = discover_controller_host(cfg).await;
        let target = match resolve_target(&host, cfg, tcfg) {
            Some(t) => t,
            None => {
                println!(">>> No reachable controller host; nothing to release");
                println!();
                println!("✓ Teardown complete.");
                return Ok(());
            }
        };

        suspend_flux(&target).await?;
        // Delete every CAPI Cluster (the management cluster included: its
        // deletion is the release). CAPT deprovisions the machine's CAPI
        // footprint; the Hardware CR stays in the Tinkerbell stack and the
        // node keeps running Talos for the operator.
        let clusters_gone = true;
        let args = kubectl_args(&target, &["get", "clusters", "-A", "-o", "name"]);
        // The listing must succeed before an empty result may be read as
        // "nothing to release" (issue #248): a failed query is an unknown
        // state, not confirmed absence, and must not release the Hardware.
        let p = probe("kubectl", &args, false, PROBE_REQUEST_TIMEOUT).await;
        let clusters: Vec<String> = match p.outcome {
            ProbeOutcome::Absent => Vec::new(),
            ProbeOutcome::Present => p
                .stdout
                .lines()
                .filter_map(|l| l.trim().rsplit('/').next())
                .map(String::from)
                .collect(),
            ProbeOutcome::Error => {
                // Unknown state: abort with a nonzero exit (issue #248), the
                // way the aws path does, instead of falling through to
                // "nothing to release" and a clean "Teardown complete.".
                bail!(
 "cannot confirm the state of the CAPI clusters on the local-talos host; the Hardware is not released and the management cluster was left intact (re-run once 'kubectl get clusters -A' works)"
                );
            }
        };
        if clusters.is_empty() {
            println!(">>> No CAPI clusters found; nothing to release");
        } else {
            for cluster in &clusters {
                println!(">>> Deleting CAPI cluster '{cluster}' (Hardware release)...");
                let args = kubectl_args(
                    &target,
                    &["delete", "cluster", cluster, "--ignore-not-found"],
                );
                let _ = run("kubectl", &to_refs(&args)).await;
            }
            println!(
                ">>> Waiting up to {}s for the clusters to be deleted...",
                tcfg.cluster_delete_timeout
            );
            let deadline = std::time::Instant::now()
                + std::time::Duration::from_secs(tcfg.cluster_delete_timeout);
            loop {
                let args = kubectl_args(&target, &["get", "clusters", "-A", "-o", "name"]);
                // The poll must distinguish "confirmed empty" from a failed
                // query (issue #248). An unknown state does not release the
                // Hardware: it leaves clusters_gone false so the host guard
                // keeps the kind cluster and the operator can re-run.
                match probe("kubectl", &args, false, PROBE_REQUEST_TIMEOUT)
                    .await
                    .outcome
                {
                    ProbeOutcome::Absent => {
                        println!("✓   All CAPI clusters deleted; Hardware released to the pool");
                        break;
                    }
                    ProbeOutcome::Present => {
                        if std::time::Instant::now() >= deadline {
                            // Deletion was not confirmed: a nonzero exit tells
                            // automation the Hardware is still reserved
                            // (issue #248).
                            bail!(
 "timed out waiting for the CAPI clusters to delete; the Hardware is not released and the management cluster was left intact (re-run once 'kubectl get clusters -A' is empty)"
                            );
                        }
                        println!(">>>   clusters still deleting...");
                        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
                    }
                    ProbeOutcome::Error => {
                        // Unknown state: abort with a nonzero exit (issue #248)
                        // instead of leaving clusters_gone false and printing
                        // a clean "Teardown complete."
                        bail!(
 "cannot confirm the deletion of the CAPI clusters on the local-talos host; the Hardware is not released and the management cluster was left intact (re-run once 'kubectl get clusters -A' works)"
                        );
                    }
                }
            }
        }

        if clusters_gone && host == ControllerHost::Kind {
            // Pre-pivot: the bootstrap kind cluster owns nothing else.
            let name = cfg.repo.bootstrap.kind_cluster.clone();
            println!(">>> Deleting kind bootstrap cluster '{name}'...");
            if run("kind", &["delete", "cluster", "--name", &name])
                .await
                .is_ok()
            {
                println!("✓   kind cluster '{name}' deleted");
            } else {
                warn("kind cluster could not be deleted – it may already be gone");
            }
        }

        println!();
        println!("✓ Teardown complete.");
        println!("!   The machine was NOT wiped: it still runs Talos. Re-use it or");
        println!("!   PXE-boot it fresh (issue #105 teardown semantics).");
        return Ok(());
    }

    // ── aws path ──────────────────────────────────────────────────────
    let host = if tcfg.aws_only {
        ControllerHost::Unreachable
    } else {
        discover_controller_host(cfg).await
    };
    if !tcfg.aws_only && host == ControllerHost::Unreachable {
        eprintln!("!   Cannot reach the management cluster. It may already be gone.");
        eprintln!("!   Running AWS orphan cleanup only. To skip this warning, set AWS_ONLY=1.");
        println!();
    }

    let target = resolve_target(&host, cfg, tcfg);

    // Steps 1-3 (k8s side): suspend Flux, delete workload clusters, wait.
    let mut clusters_gone = true;
    if !tcfg.aws_only {
        if let Some(target) = &target {
            suspend_flux(target).await?;
            clusters_gone = delete_aws_workload_clusters(
                target,
                &env.mgmt_cluster,
                tcfg.cluster_delete_timeout,
            )
            .await?;
            if !clusters_gone {
                // The script aborts here (exit 1): the remaining steps
                // must not run while CAPA is mid-deprovision, and the
                // run must report failure to automation.
                bail!("teardown aborted: CAPI workload clusters did not finish deleting; the management cluster and CAPA controller were left intact so AWS resources can continue deprovisioning");
            }
        } else {
            println!(">>> No reachable management cluster – skipping k8s steps");
        }
    }

    // Step 4: AWS orphan sweep (workloads, then the mgmt itself). The unknown
    // workload state that #248 guards against already aborts above (bail!)
    // before this point, so by the time the sweep runs the k8s side is either
    // confirmed done or was never present (unreachable controller, the
    // documented AWS-only recovery path).
    if aws_available {
        println!(">>> Cleaning up orphaned AWS resources...");
        let mut targets: Vec<AwsSweepTarget> = td
            .aws_workloads
            .iter()
            .map(AwsSweepTarget::from_workload)
            .collect();
        // The self-managed mgmt joins the sweep (post-pivot semantics):
        // its EKS cluster is removed AWS-side, never via its own API.
        if let (Some(eks), Some(prefix)) = (
            td.mgmt_eks_cluster_name.as_deref(),
            td.mgmt_iam_role_prefix.as_deref(),
        ) {
            let region = env
                .mgmt_cluster
                .split('-')
                .take(3)
                .collect::<Vec<_>>()
                .join("-");
            targets.push(AwsSweepTarget::mgmt(&region, prefix, eks));
        }
        // 4a+4b: associations + nodegroups in all regions, then wait.
        for target in &targets {
            println!(
                ">>>   [{}] cluster: {}",
                target.region, target.eks_cluster_name
            );
            cleanup_pod_identity_associations(target).await;
            cleanup_nodegroups(target).await;
        }
        for target in &targets {
            wait_nodegroups_deleted(target).await;
        }
        // 4c: EKS clusters, all regions, then wait.
        for target in &targets {
            cleanup_eks_cluster(target).await;
        }
        for target in &targets {
            wait_eks_cluster_deleted(target).await;
        }
        // 4d: RDS.
        for target in &targets {
            cleanup_rds_instance(target).await;
        }
        // 4e: VPC resources (CAPA-tagged).
        for target in &targets {
            cleanup_vpc_resources(target).await;
        }
        // 4f: S3 buckets.
        let account = capture_lossy(
            "aws",
            &[
                "sts",
                "get-caller-identity",
                "--query",
                "Account",
                "--output",
                "text",
            ],
        )
        .await
        .trim()
        .to_string();
        if !account.is_empty() {
            if let Some(pattern) = cfg.repo.teardown.s3_bucket_pattern.as_deref() {
                for target in &targets {
                    cleanup_s3_bucket(&target.bucket_name(pattern, &account), &target.region).await;
                }
            }
        } else {
            warn("Could not determine AWS account ID – skipping S3 bucket cleanup");
        }
        // 4g: IAM (per-cluster prefix sweeps + global lists).
        for target in &targets {
            cleanup_capa_iam_roles(&target.cluster_name).await;
        }
        for role in &cfg.repo.teardown.global_iam_roles {
            cleanup_iam_role(role).await;
        }
        for user in &cfg.repo.teardown.global_iam_users {
            cleanup_iam_user(user).await;
        }
        // 4h: CFN stack.
        if let Some(stack) = cfg.repo.teardown.cfn_stack_name.as_deref() {
            let regions: Vec<String> = targets.iter().map(|t| t.region.clone()).collect();
            for region in regions {
                cleanup_cfn_stack(stack, &region).await;
            }
        }
        println!("✓   AWS orphan cleanup complete");
    }

    // Steps 5-8 (k8s side) on the controller host. When the sweep above
    // removed the self-managed mgmt EKS cluster there is no reachable host
    // left whose providers, Helm releases and secrets still need cleaning
    // (the docs' "when the controller host is still reachable"); querying a
    // swept host would read the in-flight deprovision as an unknown state
    // and fail a completed teardown.
    let mgmt_swept = aws_available
        && host == ControllerHost::SelfManaged
        && td.mgmt_eks_cluster_name.is_some()
        && td.mgmt_iam_role_prefix.is_some();
    if let Some(target) = &target {
        if mgmt_swept {
            println!(
                "!   Management cluster removed by the sweep – provider and Helm cleanup skipped"
            );
        } else {
            delete_capi_providers(target, tcfg.provider_delete_timeout).await?;
            uninstall_helm_and_secrets(cfg, target).await;
        }
    }

    // Step 9: remove the controller host under the guard.
    if !tcfg.aws_only {
        let guard = HostGuard::resolve(clusters_gone, tcfg.force_host_delete);
        match guard {
            HostGuard::Refuse => {
                eprintln!();
                eprintln!("{}", HostGuard::refusal_message("the management cluster"));
            }
            HostGuard::ConfirmedGone | HostGuard::Forced => match host {
                ControllerHost::Kind => {
                    let name = cfg.repo.bootstrap.kind_cluster.clone();
                    println!(">>> Deleting kind management cluster '{name}'...");
                    if run("kind", &["delete", "cluster", "--name", &name])
                        .await
                        .is_ok()
                    {
                        println!("✓   kind cluster '{name}' deleted");
                    } else {
                        warn("kind cluster could not be deleted – it may already be gone");
                    }
                }
                ControllerHost::SelfManaged => {
                    // The mgmt EKS cluster was removed by the sweep above
                    // (or never existed); nothing k8s-side remains.
                    println!(">>> Management cluster removed via the AWS orphan sweep");
                }
                ControllerHost::Unreachable => {
                    println!(">>> No controller host to remove");
                }
            },
        }
    }

    println!();
    println!("✓ Teardown complete.");
    Ok(())
}

/// Detect the container engine the way bootstrap.sh does (docker with
/// podman re-detection, then podman). None when no engine is running.
pub async fn detect_engine() -> Option<String> {
    if let Ok(engine) = std::env::var("CONTAINER_ENGINE") {
        if !engine.is_empty() {
            if run_quiet(&engine, &["info"]).await {
                return Some(engine);
            }
            eprintln!(">>> WARNING: {engine} is unavailable; registry cleanup will be skipped");
            return None;
        }
    }
    if run_quiet("docker", &["info"]).await {
        let version = capture_lossy("docker", &["--version"]).await;
        if version.to_lowercase().contains("podman") {
            return Some("podman".into());
        }
        return Some("docker".into());
    }
    if run_quiet("podman", &["info"]).await {
        return Some("podman".into());
    }
    eprintln!(">>> WARNING: No running container engine found; registry cleanup will be skipped");
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Write an executable shell stub that records its argv to `$STUB_LOG`.
    /// `body` is the script body (run before the record line). Returns the path.
    fn install_kubectl_stub(
        dir: &std::path::Path,
        log: &std::path::Path,
        body: &str,
    ) -> std::path::PathBuf {
        let bin = dir.join("kubectl");
        let script = format!(
            "#!/usr/bin/env sh\n{body}\necho \"$@\" >> {log}\nexit 0\n",
            body = body,
            log = log.display(),
        );
        std::fs::write(&bin, script).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        bin
    }

    /// A stub whose behavior is chosen per scenario. Records argv to `$STUB_LOG`,
    /// then runs `body` (which must end with `exit N`).
    fn kubectl_stub(
        dir: &std::path::Path,
        log: &std::path::Path,
        body: &str,
    ) -> std::path::PathBuf {
        let bin = dir.join("kubectl");
        let script = format!(
            "#!/usr/bin/env sh\necho \"$@\" >> {log}\n{body}\n",
            log = log.display(),
            body = body,
        );
        std::fs::write(&bin, script).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        bin
    }

    #[tokio::test]
    async fn probe_classifies_absent_present_and_error() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let args = vec!["get".to_string(), "clusters".to_string(), "-A".to_string()];

        // Successful empty listing -> confirmed absence.
        let bin = kubectl_stub(tmp.path(), &log, "exit 0");
        assert_eq!(
            probe(&bin.to_string_lossy(), &args, false, PROBE_REQUEST_TIMEOUT)
                .await
                .outcome,
            ProbeOutcome::Absent
        );

        // Successful non-empty listing -> present, stdout returned for the caller.
        let bin = kubectl_stub(tmp.path(), &log, "echo name1\nexit 0");
        let p = probe(&bin.to_string_lossy(), &args, false, PROBE_REQUEST_TIMEOUT).await;
        assert_eq!(p.outcome, ProbeOutcome::Present);
        assert!(p.stdout.contains("name1"));

        // A named lookup reporting NotFound -> confirmed absence (single mode).
        let bin = kubectl_stub(
            tmp.path(),
            &log,
            "echo 'Error from server (NotFound): clusters not found' 1>&2\nexit 1",
        );
        assert_eq!(
            probe(&bin.to_string_lossy(), &args, true, PROBE_REQUEST_TIMEOUT)
                .await
                .outcome,
            ProbeOutcome::Absent
        );

        // A failed LIST query -> error, never absence.
        let bin = kubectl_stub(
            tmp.path(),
            &log,
            "echo 'The request could not be satisfied' 1>&2\nexit 1",
        );
        assert_eq!(
            probe(&bin.to_string_lossy(), &args, false, PROBE_REQUEST_TIMEOUT)
                .await
                .outcome,
            ProbeOutcome::Error
        );
    }

    #[tokio::test]
    async fn failed_get_does_not_read_as_gone_for_capd_workloads() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let bin = kubectl_stub(
            tmp.path(),
            &log,
            "echo 'The request could not be satisfied' 1>&2\nexit 1",
        );
        let target = K8sTarget::Kind {
            context: "ctx".into(),
        };
        // A failed query must abort (Err), not be treated as "already gone".
        let res =
            delete_capi_workloads_with(&bin.to_string_lossy(), &target, &["w".to_string()], 30)
                .await;
        assert!(
            res.is_err(),
            "a failed 'kubectl get cluster' must not skip the deletion"
        );
        let msg = res.unwrap_err().to_string();
        assert!(msg.contains("cannot confirm the state of CAPD workload cluster 'w'"));
    }

    #[tokio::test]
    async fn failed_discovery_does_not_report_aws_clusters_gone() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let bin = kubectl_stub(
            tmp.path(),
            &log,
            "echo 'The request could not be satisfied' 1>&2\nexit 1",
        );
        let target = K8sTarget::Kind {
            context: "ctx".into(),
        };
        // The pre-fix bug: a failed listing returned Ok(true) ("all clusters gone").
        let res =
            delete_aws_workload_clusters_with(&bin.to_string_lossy(), &target, "mgmt", 30).await;
        assert!(
            res.is_err(),
            "a failed 'kubectl get clusters -A' must not be read as 'no workload clusters'"
        );
    }

    #[tokio::test]
    async fn failed_poll_returns_false_during_aws_wait() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let counts = tmp.path().join("counts");
        // Discovery (first listing) shows the mgmt cluster + a workload so a delete
        // is issued; every poll after that fails (transient API outage). The function
        // must signal Ok(false) so the caller keeps the management cluster intact.
        let body = "case \"$*\" in\n  *get*clusters*)\n     n=$(cat {c} 2>/dev/null || echo 0)\n     n=$((n+1))\n     echo $n > {c}\n     if [ $n -eq 1 ]; then\n        echo 'cluster.cluster.x-k8s.io/mgmt'\n        echo 'cluster.cluster.x-k8s.io/w1'\n        exit 0\n     fi\n     echo 'The request could not be satisfied' 1>&2\n     exit 1;;\n  *delete*)\n     exit 0;;\n  *)\n     exit 0;;\nesac";
        let replaced = body.replace("{c}", &counts.display().to_string());
        let bin = kubectl_stub(tmp.path(), &log, &replaced);
        let target = K8sTarget::Kind {
            context: "ctx".into(),
        };
        let res =
            delete_aws_workload_clusters_with(&bin.to_string_lossy(), &target, "mgmt", 600).await;
        assert_eq!(
            res.as_ref().ok(),
            Some(&false),
            "a failed poll must return Ok(false) (unknown state), not Ok(true)"
        );
    }

    #[tokio::test]
    async fn confirmed_gone_still_returns_true_for_aws() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let counts = tmp.path().join("counts");
        // Discovery (first listing) shows the mgmt cluster + a workload so a delete
        // is issued; the following poll confirms only the mgmt cluster remains
        // (workloads gone). This is the happy path that must keep returning Ok(true).
        let body = "case \"$*\" in\n  *get*clusters*)\n     n=$(cat {c} 2>/dev/null || echo 0)\n     n=$((n+1))\n     echo $n > {c}\n     if [ $n -eq 1 ]; then\n        echo 'cluster.cluster.x-k8s.io/mgmt'\n        echo 'cluster.cluster.x-k8s.io/w1'\n     else\n        echo 'cluster.cluster.x-k8s.io/mgmt'\n     fi\n     exit 0;;\n  *delete*)\n     exit 0;;\n  *)\n     exit 0;;\nesac";
        let replaced = body.replace("{c}", &counts.display().to_string());
        let bin = kubectl_stub(tmp.path(), &log, &replaced);
        let target = K8sTarget::Kind {
            context: "ctx".into(),
        };
        let res =
            delete_aws_workload_clusters_with(&bin.to_string_lossy(), &target, "mgmt", 600).await;
        assert_eq!(
            res.as_ref().ok(),
            Some(&true),
            "a confirmed-empty poll must still return Ok(true)"
        );
    }

    #[tokio::test]
    async fn kind_target_binds_the_explicit_context_not_the_current_one() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let bin = install_kubectl_stub(tmp.path(), &log, "true");
        let target = K8sTarget::Kind {
            context: "kind-mgmt".into(),
        };
        suspend_flux_with(&bin.to_string_lossy(), &target)
            .await
            .unwrap();
        let recorded = std::fs::read_to_string(&log).unwrap();
        assert!(
            recorded.contains("--context kind-mgmt"),
            "kind teardown must pass --context, got: {recorded}"
        );
        assert!(
            !recorded.contains("--kubeconfig"),
            "kind path must not use a kubeconfig"
        );
    }

    #[tokio::test]
    async fn self_managed_target_binds_the_kubeconfig() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let bin = install_kubectl_stub(tmp.path(), &log, "true");
        let kc = tmp.path().join("mgmt.yaml");
        std::fs::write(&kc, "apiVersion: v1\n").unwrap();
        let target = K8sTarget::SelfManaged {
            kubeconfig: kc.to_string_lossy().into_owned(),
        };
        suspend_flux_with(&bin.to_string_lossy(), &target)
            .await
            .unwrap();
        let recorded = std::fs::read_to_string(&log).unwrap();
        assert!(recorded.contains(&format!("--kubeconfig {}", kc.display())));
        assert!(
            !recorded.contains("--context"),
            "self-managed path must not use --context"
        );
    }

    #[tokio::test]
    async fn helm_targets_kind_with_kube_context_not_bare_context() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        // A stub helm that records its argv and reports a live release, so the
        // uninstall path (not just the status probe) is exercised.
        let helm = tmp.path().join("helm");
        let script = format!(
            "#!/usr/bin/env sh\necho \"$@\" >> {log}\necho 'NAME: flux\nSTATUS: deployed'\nexit 0\n",
            log = log.display(),
        );
        std::fs::write(&helm, script).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&helm, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let target = K8sTarget::Kind {
            context: "ctx".into(),
        };
        uninstall_helm_with(&helm.to_string_lossy(), &["flux"], "flux-system", &target).await;
        let recorded = std::fs::read_to_string(&log).unwrap();
        assert!(
            recorded.contains("--kube-context ctx"),
            "helm must receive --kube-context, got: {recorded}"
        );
        assert!(
            !recorded.split_whitespace().any(|t| t == "--context"),
            "helm must not receive the bare kubectl --context flag, got: {recorded}"
        );
        assert!(
            recorded.contains("uninstall flux"),
            "the status probe must find the release so the uninstall runs, got: {recorded}"
        );
    }

    #[tokio::test]
    async fn failed_flux_listing_aborts_suspension() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let bin = kubectl_stub(
            tmp.path(),
            &log,
            "echo 'The request could not be satisfied' 1>&2\nexit 1",
        );
        let target = K8sTarget::Kind {
            context: "ctx".into(),
        };
        // A failed listing must abort, not read as "no Kustomizations".
        let res = suspend_flux_with(&bin.to_string_lossy(), &target).await;
        assert!(
            res.is_err(),
            "a failed 'kubectl get kustomizations' must not skip suspension"
        );
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("cannot list the Flux Kustomizations"));
    }

    #[tokio::test]
    async fn failed_provider_listing_aborts_provider_deletion() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let bin = kubectl_stub(
            tmp.path(),
            &log,
            "echo 'The request could not be satisfied' 1>&2\nexit 1",
        );
        let target = K8sTarget::Kind {
            context: "ctx".into(),
        };
        // A failed provider listing must abort, not read as "no providers".
        let res = delete_capi_providers_with(&bin.to_string_lossy(), &target, 30).await;
        assert!(
            res.is_err(),
            "a failed provider listing must not be read as 'no providers'"
        );
        assert!(res
            .unwrap_err()
            .to_string()
            .contains("cannot list the CAPI addonproviders providers"));
    }

    #[tokio::test]
    async fn missing_resource_type_reads_as_confirmed_absence() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        // A reachable API server stating the kind is not installed is a
        // confirmed absence, not a failed query (the CRDs were uninstalled
        // by an earlier teardown run).
        let bin = kubectl_stub(
            tmp.path(),
            &log,
            "echo \"error: the server doesn't have a resource type \\\"addonproviders\\\"\" 1>&2\nexit 1",
        );
        let args = vec![
            "get".to_string(),
            "addonproviders.operator.cluster.x-k8s.io".to_string(),
            "-A".to_string(),
        ];
        // List form (provider listing, Flux Kustomization listing).
        let p = probe(&bin.to_string_lossy(), &args, false, PROBE_REQUEST_TIMEOUT).await;
        assert_eq!(
            p.outcome,
            ProbeOutcome::Absent,
            "an absent resource type must read as confirmed absence, not an error"
        );
        // Named lookup form (a workload cluster after its CRD was removed).
        let p = probe(&bin.to_string_lossy(), &args, true, PROBE_REQUEST_TIMEOUT).await;
        assert_eq!(p.outcome, ProbeOutcome::Absent);
    }

    #[tokio::test]
    async fn probe_passes_the_request_timeout_to_kubectl() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let bin = kubectl_stub(tmp.path(), &log, "exit 0");
        let args = vec!["get".to_string(), "clusters".to_string(), "-A".to_string()];
        probe(&bin.to_string_lossy(), &args, false, PROBE_REQUEST_TIMEOUT).await;
        let recorded = std::fs::read_to_string(&log).unwrap();
        assert!(
            recorded.contains("--request-timeout=30s"),
            "probes must bound the query with --request-timeout, got: {recorded}"
        );
    }

    #[test]
    fn manual_teardown_is_refused_with_text() {
        let td = crate::config::TeardownEnv {
            manual: Some("hand".into()),
            ..Default::default()
        };
        let msg = manual_teardown_refusal("azure", &td).unwrap();
        assert!(msg.starts_with("teardown is manual for the 'azure' environment:"));
        assert!(msg.ends_with("hand"));
        assert!(manual_teardown_refusal("aws", &crate::config::TeardownEnv::default()).is_none());
    }

    #[test]
    fn guard_resolves_the_script_contract() {
        // CLUSTERS_CONFIRMED_GONE / FORCE_KIND_DELETE truth table.
        assert_eq!(HostGuard::resolve(true, false), HostGuard::ConfirmedGone);
        assert_eq!(HostGuard::resolve(false, false), HostGuard::Refuse);
        assert_eq!(HostGuard::resolve(false, true), HostGuard::Forced);
        // Force wins even when clusters are gone (Forced, not ConfirmedGone).
        assert_eq!(HostGuard::resolve(true, true), HostGuard::Forced);
    }

    #[test]
    fn refusal_message_keeps_the_script_wording() {
        let msg = HostGuard::refusal_message("the management cluster");
        assert!(msg.contains("Refusing to delete the management cluster"));
        assert!(msg.contains("FORCE_KIND_DELETE=1"));
        assert!(msg.contains("kubectl get clusters -A' is empty"));
    }

    #[test]
    fn s3_bucket_name_substitutes_the_config_pattern() {
        assert_eq!(
            s3_bucket_name(
                "krops-{account_id}-{cluster_name}-data",
                "123456789012",
                "eu-north-1-workload"
            ),
            "krops-123456789012-eu-north-1-workload-data"
        );
    }

    #[test]
    fn capa_tag_key_matches_the_provider_format() {
        assert_eq!(
            capa_tag_key("eu-north-1-workload"),
            "sigs.k8s.io/cluster-api-provider-aws/cluster/eu-north-1-workload"
        );
    }

    #[test]
    fn teardown_knobs_follow_shell_boolean_semantics() {
        // Literal "1" enables; anything else (0, yes, true) does not.
        let on = |v: Option<&str>| {
            TeardownConfig::from_env(|name| {
                if name == "AWS_ONLY" {
                    v.map(String::from)
                } else {
                    None
                }
            })
            .unwrap()
            .aws_only
        };
        assert!(on(Some("1")));
        assert!(!on(Some("0")));
        assert!(!on(Some("true")));
        assert!(!on(None));
    }

    #[test]
    fn teardown_timeouts_parse_and_default() {
        let cfg = TeardownConfig::from_env(|_| None).unwrap();
        assert_eq!(cfg.cluster_delete_timeout, 1200);
        assert_eq!(cfg.provider_delete_timeout, 300);
        let cfg = TeardownConfig::from_env(|name| {
            (name == "CLUSTER_DELETE_TIMEOUT").then(|| "60".to_string())
        })
        .unwrap();
        assert_eq!(cfg.cluster_delete_timeout, 60);
    }

    #[test]
    fn teardown_rejects_non_numeric_timeouts() {
        assert!(TeardownConfig::from_env(|name| {
            (name == "CLUSTER_DELETE_TIMEOUT").then(|| "forever".to_string())
        })
        .is_err());
    }

    #[test]
    fn aws_sweep_target_derives_the_mgmt_entry() {
        let t = AwsSweepTarget::mgmt(
            "eu-north-1",
            "eu-north-1-management",
            "default_eu-north-1-management-control-plane",
        );
        assert_eq!(t.capa_tag_key(), capa_tag_key("eu-north-1-management"));
        assert_eq!(
            t.bucket_name("krops-{account_id}-{cluster_name}-data", "acct"),
            "krops-acct-eu-north-1-management-data"
        );
    }
}

#[cfg(test)]
mod jsonpath_tests {
    use super::NS_NAME_JSONPATH;

    #[test]
    fn ns_name_jsonpath_bytes_match_the_live_verified_form() {
        // Byte-exact argv: kubectl rejects the over-escaped form
        // ('unrecognized character in action: U+005C'). Verified live
        // against a kind cluster: this exact string prints ns/name per
        // line; the over-escaped variant errors and capture_lossy
        // silently returns an empty listing.
        assert_eq!(
            NS_NAME_JSONPATH,
            "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{\"\\n\"}{end}"
        );
        // The decoded bytes contain exactly one backslash (before n).
        assert_eq!(NS_NAME_JSONPATH.matches('\\').count(), 1);
    }
}
