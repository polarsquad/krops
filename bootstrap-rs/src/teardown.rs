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
pub async fn suspend_flux(kubeconfig: Option<&str>) -> Result<()> {
    println!(">>> Suspending Flux Kustomizations to prevent re-reconciliation...");
    let names = capture_lossy(
        "kubectl",
        &kubectl_cmd(
            kubeconfig,
            &[
                "get",
                "kustomizations.kustomize.toolkit.fluxcd.io",
                "-n",
                "flux-system",
                "-o",
                "name",
            ],
        ),
    )
    .await;
    if names.trim().is_empty() {
        println!("!   No Flux Kustomizations found – skipping suspension");
        return Ok(());
    }
    let mut failed = 0;
    for ks in names.lines().filter_map(|l| l.trim().rsplit('/').next()) {
        if run(
            "kubectl",
            &kubectl_cmd(
                kubeconfig,
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
            ),
        )
        .await
        .is_err()
        {
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
    kubeconfig: Option<&str>,
    workloads: &[String],
    timeout_secs: u64,
) -> Result<()> {
    for cluster in workloads {
        println!(">>> Deleting CAPD workload cluster '{cluster}'...");
        let exists = run_quiet(
            "kubectl",
            &kubectl_cmd(kubeconfig, &["get", "cluster", cluster, "-n", "default"]),
        )
        .await;
        if !exists {
            println!("!   Cluster '{cluster}' not found – skipping");
            continue;
        }
        run(
            "kubectl",
            &kubectl_cmd(
                kubeconfig,
                &[
                    "delete",
                    "cluster",
                    cluster,
                    "-n",
                    "default",
                    "--wait=false",
                ],
            ),
        )
        .await
        .with_context(|| format!("failed to delete workload cluster '{cluster}'"))?;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
        loop {
            let gone = !run_quiet(
                "kubectl",
                &kubectl_cmd(kubeconfig, &["get", "cluster", cluster, "-n", "default"]),
            )
            .await;
            if gone {
                println!("✓   CAPD workload cluster '{cluster}' deleted");
                break;
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
    kubeconfig: Option<&str>,
    mgmt_cluster: &str,
    timeout_secs: u64,
) -> Result<bool> {
    println!(">>> Discovering CAPI Cluster resources...");
    let listing = capture_lossy(
        "kubectl",
        &kubectl_cmd(kubeconfig, &["get", "clusters", "-A", "-o", "name"]),
    )
    .await;
    let mut workloads: Vec<String> = listing
        .lines()
        .filter_map(|l| l.trim().rsplit('/').next())
        .filter(|n| *n != mgmt_cluster)
        .map(String::from)
        .collect();
    workloads.sort();
    workloads.dedup();
    if workloads.is_empty() {
        println!(">>> No CAPI workload clusters found – skipping cluster deletion");
        return Ok(true);
    }
    for cluster in &workloads {
        println!(">>>   Deleting cluster: {cluster}");
        let _ = run(
            "kubectl",
            &kubectl_cmd(
                kubeconfig,
                &["delete", "cluster", cluster, "--ignore-not-found"],
            ),
        )
        .await;
    }
    println!(">>> Waiting up to {timeout_secs}s for the workload clusters to be deleted...");
    println!(">>> (This typically takes 15–25 minutes while CAPA tears down AWS resources)");
    let start = std::time::Instant::now();
    let deadline = start + std::time::Duration::from_secs(timeout_secs);
    loop {
        let remaining: Vec<String> = capture_lossy(
            "kubectl",
            &kubectl_cmd(kubeconfig, &["get", "clusters", "-A", "-o", "name"]),
        )
        .await
        .lines()
        .filter_map(|l| l.trim().rsplit('/').next())
        .filter(|n| *n != mgmt_cluster)
        .map(String::from)
        .collect();
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
/// controllers), then wait up to the timeout.
pub async fn delete_capi_providers(kubeconfig: Option<&str>, timeout_secs: u64) {
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
        let listing = capture_lossy(
            "kubectl",
            &kubectl_cmd(kubeconfig, &["get", &full, "-A", "-o", NS_NAME_JSONPATH]),
        )
        .await;
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
            let _ = run(
                "kubectl",
                &kubectl_cmd(
                    kubeconfig,
                    &["delete", &full, name, "-n", ns, "--ignore-not-found"],
                ),
            )
            .await;
            deleted_any = true;
        }
    }
    if !deleted_any {
        println!("!   CAPI Operator CRDs not present – skipping provider deletion");
        return;
    }
    println!(">>> Waiting up to {timeout_secs}s for CAPI providers to be removed...");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    loop {
        let mut remaining = 0;
        for kind in kinds {
            let full = format!("{kind}.operator.cluster.x-k8s.io");
            let listing = capture_lossy(
                "kubectl",
                &kubectl_cmd(kubeconfig, &["get", &full, "-A", "--no-headers"]),
            )
            .await;
            remaining += listing.lines().filter(|l| !l.trim().is_empty()).count();
        }
        if remaining == 0 {
            println!("✓   All CAPI providers removed");
            return;
        }
        if std::time::Instant::now() >= deadline {
            warn("Timed out waiting for CAPI providers to be removed – continuing anyway");
            return;
        }
        println!(">>>   {remaining} provider(s) still removing...");
        tokio::time::sleep(std::time::Duration::from_secs(15)).await;
    }
}

/// Steps 6-8: helm releases then secrets (aws path, kubeconfig threaded).
pub async fn uninstall_helm_and_secrets(cfg: &Config, kubeconfig: Option<&str>) {
    for release in ["flux", "flux-operator"] {
        println!(">>> Uninstalling {release} Helm release...");
        let kc: Vec<&str> = match kubeconfig {
            Some(k) => vec!["--kubeconfig", k],
            None => vec![],
        };
        let status = {
            let mut args = kc.clone();
            args.extend_from_slice(&["status", release, "-n", "flux-system"]);
            capture_lossy("helm", &args).await
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
            "flux-system",
            "--wait",
            "--timeout",
            "5m0s",
        ]);
        if run("helm", &args).await.is_ok() {
            println!("✓   {release} Helm release uninstalled");
        } else {
            warn(&format!("{release} Helm release could not be uninstalled"));
        }
    }

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
        let _ = run(
            "kubectl",
            &kubectl_cmd(
                kubeconfig,
                &[
                    "delete",
                    "secret",
                    secret,
                    "-n",
                    namespace,
                    "--ignore-not-found",
                ],
            ),
        )
        .await;
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

/// The full teardown run. Mirrors teardown.sh's flow with the
/// post-pivot controller-host resolution.
#[allow(clippy::too_many_arguments)]
pub async fn run_teardown(cfg: &Config, tcfg: &TeardownConfig) -> Result<()> {
    let env = &cfg.environment;
    let td = &env.teardown;

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
            delete_capi_workloads(Some(kc), &td.capi_workloads, 300).await?;
        } else if kind_present {
            delete_capi_workloads(None, &td.capi_workloads, 300).await?;
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
        let kc: Option<String> = match &host {
            ControllerHost::Kind => None,
            ControllerHost::SelfManaged => {
                Some(tcfg.mgmt_kubeconfig.to_string_lossy().into_owned())
            }
            ControllerHost::Unreachable => {
                println!(">>> No reachable controller host; nothing to release");
                println!();
                println!("✓ Teardown complete.");
                return Ok(());
            }
        };

        if let Some(kc) = kc.as_deref() {
            suspend_flux(Some(kc)).await?;
        } else {
            suspend_flux(None).await?;
        }
        // Delete every CAPI Cluster (the management cluster included: its
        // deletion is the release). CAPT deprovisions the machine's CAPI
        // footprint; the Hardware CR stays in the Tinkerbell stack and the
        // node keeps running Talos for the operator.
        let mut clusters_gone = true;
        let listing = capture_lossy(
            "kubectl",
            &kubectl_cmd(kc.as_deref(), &["get", "clusters", "-A", "-o", "name"]),
        )
        .await;
        let clusters: Vec<String> = listing
            .lines()
            .filter_map(|l| l.trim().rsplit('/').next())
            .map(String::from)
            .collect();
        if clusters.is_empty() {
            println!(">>> No CAPI clusters found; nothing to release");
        } else {
            for cluster in &clusters {
                println!(">>> Deleting CAPI cluster '{cluster}' (Hardware release)...");
                let _ = run(
                    "kubectl",
                    &kubectl_cmd(
                        kc.as_deref(),
                        &["delete", "cluster", cluster, "--ignore-not-found"],
                    ),
                )
                .await;
            }
            println!(
                ">>> Waiting up to {}s for the clusters to be deleted...",
                tcfg.cluster_delete_timeout
            );
            let deadline = std::time::Instant::now()
                + std::time::Duration::from_secs(tcfg.cluster_delete_timeout);
            loop {
                let remaining = capture_lossy(
                    "kubectl",
                    &kubectl_cmd(kc.as_deref(), &["get", "clusters", "-A", "-o", "name"]),
                )
                .await;
                if remaining.trim().is_empty() {
                    println!("✓   All CAPI clusters deleted; Hardware released to the pool");
                    break;
                }
                if std::time::Instant::now() >= deadline {
                    eprintln!("!   Timed out waiting for CAPI clusters to delete; aborting.");
                    eprintln!("!   The management cluster was left intact; re-run teardown once");
                    eprintln!("!   'kubectl get clusters -A' is empty.");
                    clusters_gone = false;
                    break;
                }
                println!(">>>   clusters still deleting...");
                tokio::time::sleep(std::time::Duration::from_secs(30)).await;
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

    let kc: Option<String> = match &host {
        ControllerHost::Kind => None,
        ControllerHost::SelfManaged => Some(tcfg.mgmt_kubeconfig.to_string_lossy().into_owned()),
        ControllerHost::Unreachable => None,
    };

    // Steps 1-3 (k8s side): suspend Flux, delete workload clusters, wait.
    let mut clusters_gone = true;
    if !tcfg.aws_only {
        if host != ControllerHost::Unreachable {
            suspend_flux(kc.as_deref()).await?;
            clusters_gone = delete_aws_workload_clusters(
                kc.as_deref(),
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

    // Step 4: AWS orphan sweep (workloads, then the mgmt itself).
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

    // Steps 5-8 (k8s side) on the controller host.
    if !tcfg.aws_only && host != ControllerHost::Unreachable {
        delete_capi_providers(kc.as_deref(), tcfg.provider_delete_timeout).await;
        uninstall_helm_and_secrets(cfg, kc.as_deref()).await;
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
