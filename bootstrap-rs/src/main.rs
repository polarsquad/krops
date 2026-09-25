//! krops-bootstrap – One-time imperative bootstrap for the management cluster.
//! Everything after this program runs is driven by GitOps (Flux).
//!
//! Rust port of `bootstrap.sh` including its default exit: unless
//! BOOTSTRAP_PIVOT=0, the bootstrap continues into the port of `pivot.sh`
//! (issue #95), which moves the CAPI inventory into the self-managed
//! management cluster and then deletes the kind bootstrap cluster.
//!
//! The CLI surface is the script's surface:
//! a positional profile, `--recreate`, and the environment. Unlike the
//! script, reruns are safe by default: an existing healthy 'mgmt' cluster
//! is reused and every step is idempotent, so a partially failed bootstrap
//! can be resumed by rerunning. Pass --recreate to delete and rebuild the
//! cluster instead.
//!
//! Everything repository-specific (names, paths, chart pins, environments)
//! comes from `bootstrap.toml` (see `config.rs`, issue #98); the binary is
//! a generic bootstrap engine and krops is its first consumer.

mod config;
mod engine;
mod teardown;

use config::{BootstrapConfig, Environment, SyncSource};

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{bail, ensure, Context, Result};
use clap::Parser;
use serde_json::json;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

const HTTP_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const HTTP_REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// curl's documented transient statuses: the set `--retry` retries.
const CURL_TRANSIENT_STATUSES: [u16; 6] = [408, 429, 500, 502, 503, 504];

// Defaults mirroring bootstrap.sh's `${VAR:-default}` values, plus the node
// Ready wait the script hardcodes (kubectl wait --timeout=120s).
const DEFAULT_REGISTRY_PORT: u16 = 5001;
const DEFAULT_REGISTRY_READY_RETRIES: u32 = 120;
const DEFAULT_LOCAL_RECONCILE_TIMEOUT: &str = "15m";
const DEFAULT_GITHUB_USER: &str = "git";
const DEFAULT_AGE_KEY_FILE: &str = "age.agekey";
const DEFAULT_OCI_REPOSITORY: &str = "krops";
const DEFAULT_OCI_TAG: &str = "latest";
const NODE_READY_TIMEOUT: &str = "120s";

// Pivot defaults mirroring pivot.sh's `${VAR:-default}` values. The
// repository-specific defaults (mgmt cluster names, ready timeouts, kind
// names, contexts, namespaces) come from bootstrap.toml instead.
const DEFAULT_MGMT_KUBECONFIG_RELATIVE: &str = ".kube/krops-mgmt.yaml";
const DEFAULT_MGMT_POLL_INTERVAL: u64 = 10;
/// Phase 2 node readiness budget (the previous bare `kubectl wait node
/// --all --timeout=15m`). The node pool registers its first nodes after the
/// control plane goes ACTIVE, so the wait polls through a nodeless target
/// (issue #349) within this same budget.
const MGMT_NODE_READY_TIMEOUT: &str = "15m";

// ── CLI ───────────────────────────────────────────────────────────────────────

/// Resolve the active environment name the way bootstrap.sh does
/// (`PROFILE="${KROPS_PROFILE:-${1:-aws}}"`): a non-empty
/// KROPS_PROFILE wins over the positional argument, then the config's
/// bootstrap.default-environment. clap resolves positional-over-env (the
/// opposite), so the env var is read manually instead of #[arg(env)].
/// The name must be an [environments.*] section of bootstrap.toml; the
/// section name is authoritative (issue #98).
fn resolve_environment(
    env: Option<&str>,
    positional: Option<&str>,
    config: &BootstrapConfig,
) -> Result<String> {
    let name = if let Some(value) = env.filter(|v| !v.is_empty()) {
        value
    } else {
        positional.unwrap_or(&config.bootstrap.default_environment)
    };
    config.environment(name).map(|_| name.to_string())
}

/// One-time imperative bootstrap for the krops management cluster.
///
/// Behavioral port of bootstrap.sh: the CLI surface is the script's
/// surface — a positional profile, `--recreate`, and the environment.
/// Every `${VAR:-default}` knob the script reads is read the same way
/// (see `Config`); the expanded flag interface is deferred for separate
/// review in a follow-up.
#[derive(Parser, Debug)]
#[command(name = "krops-bootstrap", version, about)]
struct Cli {
    /// Deployment profile (a non-empty KROPS_PROFILE takes precedence
    /// over this argument, matching bootstrap.sh; default: the config's
    /// bootstrap.default-environment). Valid names are the
    /// [environments.*] sections of bootstrap.toml.
    profile: Option<String>,

    /// Delete and recreate an existing 'mgmt' cluster instead of reusing it.
    /// Required when the profile or the kind/registry configuration changed
    /// since the cluster was created (the reuse path does not detect drift).
    #[arg(long)]
    recreate: bool,

    /// Tear down everything the bootstrap created (issue #100): the port
    /// of teardown.sh with post-pivot semantics. Knobs stay env-only
    /// (AWS_ONLY, FORCE_KIND_DELETE, CLUSTER_DELETE_TIMEOUT,
    /// PROVIDER_DELETE_TIMEOUT), matching the script's interface.
    #[command(subcommand)]
    command: Option<SubCommand>,
}

#[derive(clap::Subcommand, Debug)]
enum SubCommand {
    /// Destroy all infrastructure the bootstrap created, in reverse order.
    Teardown {
        /// Deployment profile (KROPS_PROFILE takes precedence, as
        /// everywhere else; default: bootstrap.default-environment).
        profile: Option<String>,
    },
}

/// Resolved run configuration: the environment knobs bootstrap.sh and
/// pivot.sh read with `${VAR:-default}` semantics — unset and empty both
/// fall back to the default, exactly as in the scripts. Not clap args: this
/// port reproduces the scripts' behavior, and their interface is env-only
/// (including the pivot opt-outs, per the #95 entry-point decision).
#[derive(Debug)]
struct Config {
    /// The parsed bootstrap.toml (repository-owned values).
    repo: BootstrapConfig,
    /// The resolved [environments.*] section name.
    profile: String,
    /// The resolved environment section.
    environment: Environment,
    recreate: bool,
    registry_port: u16,
    registry_ready_retries: u32,
    local_reconcile_timeout: String,
    container_engine: Option<String>,
    engine_sock: Option<String>,
    toolbox: bool,
    git_repo_url: Option<String>,
    github_token: Option<String>,
    github_user: String,
    age_key_file: PathBuf,
    age_public_key: Option<String>,
    oci_repository: String,
    oci_tag: String,
    bootstrap_pivot: bool,
    pivot_skip_delete: bool,
    mgmt_kubeconfig: PathBuf,
    mgmt_ready_timeout: String,
    mgmt_poll_interval: u64,
    bootstrap_kubecontext: String,
}

impl Config {
    /// The environment's sequence archetype (issue #98 decision 3): the
    /// names are the binary's opinionated contract, kept as literals.
    fn is_local(&self) -> bool {
        self.profile == "local-host"
    }

    /// Address the local registry from the process running this binary.
    /// Host runs use the published port; toolbox runs share the kind network.
    fn registry_endpoint(&self) -> (String, u16) {
        if self.toolbox {
            (self.repo.bootstrap.registry_name.clone(), 5000)
        } else {
            ("localhost".to_string(), self.registry_port)
        }
    }

    /// CAPD kubeconfigs already contain a kind-network endpoint. Host runs
    /// rewrite it to a published localhost port; toolbox runs keep it intact.
    fn should_rewrite_capd_endpoint(&self) -> bool {
        self.is_local() && !self.toolbox
    }

    /// Resolve the run configuration from the process environment.
    fn load(cli: &Cli, repo: BootstrapConfig) -> Result<Self> {
        Self::from_env(cli, repo, |name| std::env::var(name).ok())
    }

    /// Build a Config from a lookup over the script's env knobs
    /// (injectable so the resolution logic is unit-testable). An empty
    /// value behaves like unset, matching `${VAR:-default}`.
    fn from_env(
        cli: &Cli,
        repo: BootstrapConfig,
        get: impl Fn(&str) -> Option<String>,
    ) -> Result<Self> {
        let value = |name: &str| get(name).filter(|v| !v.is_empty());
        let with_default =
            |name: &str, default: &str| value(name).unwrap_or_else(|| default.to_string());
        let profile = resolve_environment(
            value("KROPS_PROFILE").as_deref(),
            cli.profile.as_deref(),
            &repo,
        )?;
        let environment = repo
            .environment(&profile)
            .with_context(|| "internal: environment vanished between resolution and lookup")?
            .clone();
        let registry_port = with_default("REGISTRY_PORT", &DEFAULT_REGISTRY_PORT.to_string())
            .parse::<u16>()
            .context("REGISTRY_PORT must be a port number (1-65535)")?;
        let registry_ready_retries = with_default(
            "REGISTRY_READY_RETRIES",
            &DEFAULT_REGISTRY_READY_RETRIES.to_string(),
        )
        .parse::<u32>()
        .context("REGISTRY_READY_RETRIES must be a non-negative integer")?;
        // Pivot knobs are validated here, not in pivot Phase 1, so a bad
        // value fails at startup instead of after the full bootstrap.
        // The per-environment default comes from bootstrap.toml.
        let mgmt_ready_timeout =
            value("MGMT_READY_TIMEOUT").unwrap_or_else(|| environment.mgmt_ready_timeout.clone());
        parse_duration_seconds(&mgmt_ready_timeout)
            .context("MGMT_READY_TIMEOUT must be a duration (40m, 2h, 90s, or bare seconds)")?;
        let mgmt_poll_interval = with_default(
            "MGMT_POLL_INTERVAL",
            &DEFAULT_MGMT_POLL_INTERVAL.to_string(),
        )
        .parse::<u64>()
        .context("MGMT_POLL_INTERVAL must be a positive integer")?;
        ensure!(
            mgmt_poll_interval > 0,
            "MGMT_POLL_INTERVAL must be a positive integer, got 0"
        );
        Ok(Config {
            recreate: cli.recreate,
            registry_port,
            registry_ready_retries,
            local_reconcile_timeout: with_default(
                "LOCAL_RECONCILE_TIMEOUT",
                DEFAULT_LOCAL_RECONCILE_TIMEOUT,
            ),
            container_engine: value("CONTAINER_ENGINE"),
            engine_sock: value("ENGINE_SOCK"),
            toolbox: with_default("KROPS_TOOLBOX", "0") == "1",
            git_repo_url: value("GIT_REPO_URL"),
            github_token: value("GITHUB_TOKEN"),
            github_user: with_default("GITHUB_USER", DEFAULT_GITHUB_USER),
            age_key_file: PathBuf::from(with_default("AGE_KEY_FILE", DEFAULT_AGE_KEY_FILE)),
            age_public_key: value("AGE_PUBLIC_KEY"),
            oci_repository: with_default("OCI_REPOSITORY", DEFAULT_OCI_REPOSITORY),
            oci_tag: with_default("OCI_TAG", DEFAULT_OCI_TAG),
            // `${BOOTSTRAP_PIVOT:-1}" = 1` semantics: only the literal "1"
            // enables; anything else opts out (pivot.sh uses the same trick
            // for PIVOT_SKIP_DELETE).
            bootstrap_pivot: with_default("BOOTSTRAP_PIVOT", "1") == "1",
            pivot_skip_delete: with_default("PIVOT_SKIP_DELETE", "0") == "1",
            mgmt_kubeconfig: value("MGMT_KUBECONFIG")
                .map(PathBuf::from)
                .unwrap_or_else(default_mgmt_kubeconfig),
            mgmt_ready_timeout,
            mgmt_poll_interval,
            bootstrap_kubecontext: with_default(
                "BOOTSTRAP_KUBECONTEXT",
                &repo.bootstrap.kind_context,
            ),
            repo,
            profile,
            environment: environment.clone(),
        })
    }
}

/// Default management kubeconfig path: `$HOME/.kube/krops-mgmt.yaml`
/// (pivot.sh `MGMT_KUBECONFIG` default).
fn default_mgmt_kubeconfig() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default()
        .join(DEFAULT_MGMT_KUBECONFIG_RELATIVE)
}

// ── Pure helpers (unit-tested) ────────────────────────────────────────────────

/// Parse `owner/repo` out of an HTTPS GitHub repository URL.
fn parse_github_repo(url: &str) -> Option<&str> {
    let rest = url.strip_prefix("https://github.com/")?;
    let repo = rest.trim_end_matches('/').trim_end_matches(".git");
    let (owner, name) = repo.split_once('/')?;
    if owner.is_empty() || name.is_empty() || name.contains('/') {
        return None;
    }
    Some(repo)
}

/// Validate an age key file's three required fields; returns missing field names.
fn validate_age_key(content: &str) -> Vec<&'static str> {
    let mut missing = Vec::new();
    if !content.lines().any(|l| l.starts_with("# created:")) {
        missing.push("# created: header");
    }
    if !content.lines().any(|l| l.starts_with("# public key:")) {
        missing.push("# public key: comment");
    }
    if !content.lines().any(|l| l.starts_with("AGE-SECRET-KEY-")) {
        missing.push("AGE-SECRET-KEY- line");
    }
    missing
}

/// Extract the public key from a validated age key file.
fn extract_age_pubkey(content: &str) -> Option<String> {
    content
        .lines()
        .find_map(|l| l.strip_prefix("# public key: "))
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// Resolve the age public key planted in the sops-age secret. The
/// `AGE_PUBLIC_KEY` override wins only when it agrees with the key file: a
/// stale override makes Flux name the secret entry after a key that matches
/// no SOPS recipient, so every `*.sops.yaml` Kustomization then fails with
/// `no keys found in sops-age secret` (issue #350). A mismatch against a
/// non-empty key-file pubkey fails fast, naming both public keys. Returns
/// `None` when neither source yields a key.
fn resolve_age_pubkey(
    override_key: Option<&str>,
    key_file_pubkey: Option<String>,
) -> Result<Option<String>> {
    let override_key = override_key.filter(|k| !k.is_empty());
    match (override_key, key_file_pubkey) {
        (Some(ovr), Some(file_key)) if ovr != file_key => bail!(
            "AGE_PUBLIC_KEY ('{ovr}') does not match the public key in the age key file ('{file_key}').\n       The override would plant the wrong key in the sops-age secret and Flux could not decrypt any *.sops.yaml.\n       Remove AGE_PUBLIC_KEY from .env or set it to the key file's public key. See docs/secrets.md."
        ),
        (Some(ovr), _) => Ok(Some(ovr.to_string())),
        (None, file_key) => Ok(file_key),
    }
}

/// Extract the numeric host port from `<engine> port` output ("0.0.0.0:32771").
fn extract_workload_port(port_output: &str) -> Option<String> {
    port_output
        .lines()
        .next()
        .and_then(|l| l.rsplit(':').next())
        .map(str::trim)
        .filter(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()))
        .map(str::to_string)
}

fn toolbox_kubeconfig_path(toolbox: bool, kubeconfig: Option<&str>) -> Result<Option<PathBuf>> {
    if !toolbox {
        return Ok(None);
    }
    let path = kubeconfig
        .filter(|value| !value.is_empty())
        .context("KUBECONFIG must name one writable file when KROPS_TOOLBOX=1")?;
    ensure!(
        !path.contains(':'),
        "KUBECONFIG must name a single file when KROPS_TOOLBOX=1"
    );
    Ok(Some(PathBuf::from(path)))
}

fn internal_kind_kubeconfig_args(name: &str) -> [&str; 5] {
    ["get", "kubeconfig", "--internal", "--name", name]
}

/// The `server:` endpoint recorded in a CAPD-exported kubeconfig.
fn capd_recorded_endpoint(kubeconfig: &str) -> Option<String> {
    kubeconfig
        .lines()
        .find_map(|l| l.trim().strip_prefix("server: "))
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// Render the kind cluster config, mirroring the script's heredoc.
/// `registry_name` comes from bootstrap.toml ([bootstrap] registry-name).
fn render_kind_config(
    is_local: bool,
    registry_port: u16,
    engine_sock: &str,
    registry_name: &str,
) -> String {
    let registry_patch = if is_local {
        format!(
            "containerdConfigPatches:\n  - |-\n    [plugins.\"io.containerd.grpc.v1.cri\".registry.mirrors.\"localhost:{registry_port}\"]\n      endpoint = [\"http://{registry_name}:5000\"]\n"
        )
    } else {
        String::new()
    };
    format!(
        "kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\n{registry_patch}nodes:\n  - role: control-plane\n    extraMounts:\n      - hostPath: {engine_sock}\n        containerPath: /var/run/docker.sock\n"
    )
}

/// Parse a duration the way pivot.sh's timeout arithmetic does: `40m`,
/// `2h`, `90s`, or a bare second count.
fn parse_duration_seconds(input: &str) -> Result<u64> {
    let s = input.trim();
    let (digits, unit): (&str, u64) = if let Some(d) = s.strip_suffix('h') {
        (d, 3600)
    } else if let Some(d) = s.strip_suffix('m') {
        (d, 60)
    } else if let Some(d) = s.strip_suffix('s') {
        (d, 1)
    } else {
        (s, 1)
    };
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        bail!("invalid duration '{input}' (expected forms like 40m, 2h, 90s, or bare seconds)");
    }
    digits
        .parse::<u64>()
        .map(|n| n * unit)
        .with_context(|| format!("duration '{input}' overflows"))
}

/// Flux Kustomization suspend patch (pivot Phase 4).
fn suspend_patch() -> serde_json::Value {
    json!({ "spec": { "suspend": true } })
}

/// Moved Cluster unpause patch (pivot Phase 4).
fn unpause_patch() -> serde_json::Value {
    json!({ "spec": { "paused": false } })
}

/// EIPs one krops EKS cluster allocates: one NAT gateway per AZ, 3 AZs.
const EIPS_PER_CLUSTER: u32 = 3;

/// Per-region EIP plan: the EIPs the run needs there, and the CAPI Cluster
/// names whose CAPA-owned EIPs (`teardown::capa_tag_key`) already count
/// toward that need.
#[derive(Debug, Default, PartialEq)]
struct EipRegionPlan {
    required: u32,
    clusters: Vec<String>,
}

/// Per-region EIP plan for an AWS environment. The management cluster's
/// region comes from stripping the "-management" suffix from its name,
/// independent of the workload regions.
fn derive_eip_requirements(
    workloads: &[config::AwsWorkload],
    mgmt_cluster: &str,
) -> Result<std::collections::BTreeMap<String, EipRegionPlan>> {
    let mut map: std::collections::BTreeMap<String, EipRegionPlan> =
        std::collections::BTreeMap::new();
    let mut add = |region: &str, cluster: &str| {
        let plan = map.entry(region.to_string()).or_default();
        plan.required += EIPS_PER_CLUSTER;
        plan.clusters.push(cluster.to_string());
    };
    for w in workloads {
        add(&w.region, &w.cluster_name);
    }
    if let Some(region) = mgmt_cluster.strip_suffix("-management") {
        add(region, mgmt_cluster);
    } else {
        bail!(
            "management cluster name '{mgmt_cluster}' does not end with '-management'; \
             cannot derive its AWS region for EIP quota check"
        );
    }
    Ok(map)
}

/// EIPs available to krops in a region: the quota minus the EIPs held by
/// anything other than the krops clusters (saturating at zero).
fn eip_available(limit: u32, allocated: u32, owned: u32) -> u32 {
    limit.saturating_sub(allocated.saturating_sub(owned))
}

/// How many EIPs a region is short, or None when the quota suffices. EIPs
/// the krops clusters already own are not counted against the requirement,
/// so a rerun after a partial or complete run passes.
fn eip_shortfall(limit: u32, allocated: u32, owned: u32, required: u32) -> Option<u32> {
    let available = eip_available(limit, allocated, owned);
    if available < required {
        Some(required - available)
    } else {
        None
    }
}

/// Tools required on PATH for the given environment's sync surface (the
/// environment names are the binary's sequence contract, issue #98
/// decision 3; the extras are the sync source's, issue #105 scope item 6).
fn required_tools(env: &Environment) -> Vec<&'static str> {
    // The binary owns the HTTP checks the scripts used curl for. curl
    // stays required for oci-sync environments anyway: the mise oci-push
    // task shells out to curl for its registry availability check. flux
    // is exercised by the local-host reconciliation watch (Step 5).
    // clusterctl and mise are pivot tools (clusterctl get kubeconfig /
    // describe / move; mise aws-credentials / oci-push) required on
    // EVERY environment. talosctl is deliberately absent for local-talos:
    // the machine is remote and talosctl is an operator convenience, not
    // a bootstrap dependency.
    let mut tools = vec!["kind", "helm", "kubectl", "clusterctl", "mise"];
    if env.sync == SyncSource::Oci {
        tools.extend(["flux", "curl"]);
    }
    if !env.pivot_sops_secrets.is_empty() {
        // The pivot decrypts these with the operator's age key (Phase 3).
        tools.push("sops");
    }
    if runs_aws_quota_preflight(env) {
        tools.push("aws");
    }
    tools
}

/// argv for the optional post-kind-create hook (issue #236): the named mise
/// task in the active profile, same invocation shape as aws-credentials.
fn post_kind_create_hook_args<'a>(profile: &'a str, task: &'a str) -> Vec<&'a str> {
    vec!["-E", profile, "run", task]
}

/// Replace `${VAR}` and `${VAR:=default}` placeholders in a pivot manifest
/// from `vars` (the ConfigMap data the bootstrap cluster's Flux already
/// reconciled). `${VAR:=default}` (issue #72) renders the configured value
/// when `VAR` is present and the default otherwise, so a placeholder can
/// carry the post-pivot value while the bootstrap cluster substitutes the
/// live one. Plain `${VAR}` placeholders with no value are left literal so
/// `remaining_manifest_vars` can name them.
fn substitute_manifest_vars(
    manifest: &str,
    vars: &std::collections::HashMap<String, String>,
) -> String {
    let mut out = String::with_capacity(manifest.len());
    let mut rest = manifest;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        rest = &rest[start + 2..];
        let Some(end) = rest.find('}') else {
            out.push_str("${");
            out.push_str(rest);
            return out;
        };
        let inner = &rest[..end];
        let (name, default) = match inner.find(":=") {
            Some(i) => (&inner[..i], Some(&inner[i + 2..])),
            None => (inner, None),
        };
        if let Some(value) = vars.get(name) {
            out.push_str(value);
        } else if let Some(d) = default {
            out.push_str(d);
        } else {
            out.push_str("${");
            out.push_str(inner);
            out.push('}');
        }
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    out
}

/// The `${VAR}` placeholder names still present in a manifest (issue #72):
/// placeholders that carry a default (`${VAR:=...}`) never count, because
/// they resolve to that default; only plain `${VAR}` names remain.
fn remaining_manifest_vars(manifest: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    let mut rest = manifest;
    while let Some(start) = rest.find("${") {
        rest = &rest[start + 2..];
        let Some(end) = rest.find('}') else { break };
        let inner = &rest[..end];
        // A default (`${VAR:=...}`) means the placeholder resolves, so only
        // plain `${VAR}` (no `:=`) counts as remaining.
        if inner.split_once(":=").is_none() && !names.contains(&inner.to_string()) {
            names.push(inner.to_string());
        }
        rest = &rest[end + 1..];
    }
    names
}

/// Merge the `data` of every ConfigMap in the Flux namespace (the values
/// the bootstrap cluster's Flux reconciled) into one substitution map for
/// pivot manifests (issue #236): the Git file carries `${VAR}` placeholders
/// until the target's own Flux runs postBuild substitution.
async fn flux_namespace_vars(flux_ns: &str) -> Result<std::collections::HashMap<String, String>> {
    let json_text = capture(
        "kubectl",
        &[
            "get",
            "configmaps",
            "--namespace",
            flux_ns,
            "--output",
            "json",
        ],
    )
    .await?;
    let parsed: serde_json::Value =
        serde_json::from_str(&json_text).context("failed to parse kubectl configmaps output")?;
    let mut vars = std::collections::HashMap::new();
    if let Some(items) = parsed.get("items").and_then(|v| v.as_array()) {
        for item in items {
            if let Some(data) = item.get("data").and_then(|d| d.as_object()) {
                for (key, value) in data {
                    if let Some(s) = value.as_str() {
                        vars.insert(key.clone(), s.to_string());
                    }
                }
            }
        }
    }
    Ok(vars)
}

/// Whether the GitHub/age preflight (PAT, repo branch probe, sops age
/// key) must run: gated on the sync source (issue #105 scope item 6),
/// not the profile name. AWS-only credential steps stay profile-gated.
fn runs_github_preflight(cfg: &Config) -> bool {
    cfg.environment.sync == SyncSource::Github
}

/// The EIP quota preflight covers every AWS-kind environment, including
/// one whose only cluster is the management cluster.
fn runs_aws_quota_preflight(env: &Environment) -> bool {
    env.kind == "aws"
}

// ── Process helpers ───────────────────────────────────────────────────────────

/// Is `cmd` an executable file somewhere on PATH?
fn command_exists(cmd: &str) -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| is_executable_file(&dir.join(cmd)))
}

#[cfg(unix)]
fn is_executable_file(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    p.is_file()
        && p.metadata()
            .map(|m| m.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable_file(p: &Path) -> bool {
    p.is_file()
}

/// Run a command with inherited stdio; error if it exits nonzero.
///
/// Secret safety: no secret material is ever passed on argv anywhere in this
/// program (secrets travel via stdin manifests), so command lines are safe to
/// echo verbatim in errors.
pub(crate) async fn run(cmd: &str, args: &[&str]) -> Result<()> {
    let status = Command::new(cmd)
        .args(args)
        .status()
        .await
        .with_context(|| format!("failed to spawn '{cmd}'"))?;
    if !status.success() {
        bail!("'{cmd} {}' failed with {status}", args.join(" "));
    }
    Ok(())
}

/// Run a command silently; report only whether it succeeded.
pub(crate) async fn run_quiet(cmd: &str, args: &[&str]) -> bool {
    Command::new(cmd)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Run a command and capture stdout (stderr inherited); error on nonzero exit.
pub(crate) async fn capture(cmd: &str, args: &[&str]) -> Result<String> {
    let out = Command::new(cmd)
        .args(args)
        .stderr(Stdio::inherit())
        .output()
        .await
        .with_context(|| format!("failed to spawn '{cmd}'"))?;
    if !out.status.success() {
        bail!("'{cmd} {}' failed with {}", args.join(" "), out.status);
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Run a command and capture stdout, ignoring the exit status (like `cmd || true`).
pub(crate) async fn capture_lossy(cmd: &str, args: &[&str]) -> String {
    Command::new(cmd)
        .args(args)
        .stderr(Stdio::null())
        .output()
        .await
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default()
}

/// Run a command with `input` piped to stdin and stdio otherwise inherited.
/// Error messages include argv only, never stdin content, so manifests
/// containing secret material are safe to pass here.
pub(crate) async fn run_with_stdin(cmd: &str, args: &[&str], input: &str) -> Result<()> {
    let mut child = Command::new(cmd)
        .args(args)
        .stdin(Stdio::piped())
        .spawn()
        .with_context(|| format!("failed to spawn '{cmd}'"))?;
    child
        .stdin
        .take()
        .context("child stdin unavailable")?
        .write_all(input.as_bytes())
        .await?;
    let status = child.wait().await?;
    if !status.success() {
        bail!("'{cmd} {}' failed with {status}", args.join(" "));
    }
    Ok(())
}

/// kubectl args with an optional target kubeconfig (None = the current
/// context, i.e. the kind cluster; Some = the pivot target).
pub(crate) fn kubectl_cmd<'a>(kubeconfig: Option<&'a str>, args: &[&'a str]) -> Vec<&'a str> {
    let mut full = Vec::with_capacity(args.len() + 2);
    if let Some(kc) = kubeconfig {
        full.push("--kubeconfig");
        full.push(kc);
    }
    full.extend_from_slice(args);
    full
}

/// Apply a Kubernetes manifest (JSON) via `kubectl apply -f -`, against the
/// current context or a target kubeconfig. Idempotent, and keeps secret
/// values off argv.
async fn kubectl_apply(kubeconfig: Option<&str>, manifest: &serde_json::Value) -> Result<()> {
    run_with_stdin(
        "kubectl",
        &kubectl_cmd(kubeconfig, &["apply", "-f", "-"]),
        &manifest.to_string(),
    )
    .await
}

/// Kills the wrapped child process when dropped (best-effort).
struct ChildGuard(tokio::process::Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.start_kill();
    }
}

// ── Preflight ─────────────────────────────────────────────────────────────────

struct GithubContext {
    git_repo_url: String,
    github_user: String,
    github_token: String,
    age_key_content: String,
    age_pubkey: String,
}

struct Preflight {
    engine: String,
    engine_sock: String,
    github: Option<GithubContext>,
}

async fn preflight_checks(cfg: &Config, http: &reqwest::Client) -> Result<Preflight> {
    // Report every missing tool at once instead of failing on the first.
    let missing: Vec<&str> = required_tools(&cfg.environment)
        .into_iter()
        .filter(|t| !command_exists(t))
        .collect();
    if !missing.is_empty() {
        bail!("missing required tools in PATH: {}", missing.join(", "));
    }

    let github = if runs_github_preflight(cfg) {
        Some(preflight_github(cfg, http).await?)
    } else {
        None
    };

    if runs_aws_quota_preflight(&cfg.environment) {
        preflight_aws_quotas(cfg).await?;
    }

    let resolved = engine::resolve(
        cfg.container_engine.clone(),
        cfg.engine_sock.clone(),
        cfg.toolbox,
    )
    .await?;

    Ok(Preflight {
        engine: resolved.engine,
        engine_sock: resolved.engine_sock,
        github,
    })
}

async fn preflight_github(cfg: &Config, http: &reqwest::Client) -> Result<GithubContext> {
    let github_token = cfg
        .github_token
        .clone()
        .context("GITHUB_TOKEN must be set (a PAT with read access to the repo)")?;
    let git_repo_url = cfg
        .git_repo_url
        .clone()
        .context("GIT_REPO_URL must be set")?;

    // GITHUB_USER is used in the Flux GitHub secret for repo clone authentication.
    let github_user = cfg.github_user.clone();

    let github_repo = parse_github_repo(&git_repo_url)
        .context("GIT_REPO_URL must be an HTTPS GitHub repository URL")?;

    let branch_path = cfg.repo.bootstrap.git_branch.replace('/', "%2F");
    let url = format!("https://api.github.com/repos/{github_repo}/branches/{branch_path}");
    let status = http
        .get(&url)
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {github_token}"))
        .send()
        .await
        .map(|r| r.status().as_u16())
        .unwrap_or(0);
    if status != 200 {
        bail!(
            "GitHub repository or branch '{}' is unavailable at '{git_repo_url}' (HTTP {status})",
            cfg.repo.bootstrap.git_branch
        );
    }

    let age_key_file = cfg.age_key_file.clone();
    if !age_key_file.is_file() {
        bail!(
            "age key file not found at '{}'.\n       Generate one with:  mise run sops-keygen\n       and add its PUBLIC key to .sops.yaml. See docs/secrets.md.",
            age_key_file.display()
        );
    }

    // Validate age key file format first (before attempting to extract the
    // public key). This avoids silently proceeding with a malformed file.
    let age_key_content = std::fs::read_to_string(&age_key_file)
        .with_context(|| format!("failed to read '{}'", age_key_file.display()))?;
    let missing_fields = validate_age_key(&age_key_content);
    if !missing_fields.is_empty() {
        bail!(
            "'{}' is not a valid age key file.\n       Missing: {}",
            age_key_file.display(),
            missing_fields.join(", ")
        );
    }

    // Now safely extract the public key (validation already passed). A set
    // AGE_PUBLIC_KEY still wins, but only after a cross-check against the
    // key file: a stale override otherwise plants the wrong key in the
    // sops-age secret and Flux cannot decrypt any *.sops.yaml (issue #350).
    let age_pubkey = resolve_age_pubkey(
        cfg.age_public_key.as_deref(),
        extract_age_pubkey(&age_key_content),
    )?
    .with_context(|| {
        format!(
            "Cannot determine age public key from '{}' or from AGE_PUBLIC_KEY env var.\n       Set AGE_PUBLIC_KEY in .env, or regenerate the key with: mise run sops-keygen",
            age_key_file.display()
        )
    })?;

    Ok(GithubContext {
        git_repo_url,
        github_user,
        github_token,
        age_key_content,
        age_pubkey,
    })
}

async fn preflight_aws_quotas(cfg: &Config) -> Result<()> {
    let workloads = &cfg.environment.teardown.aws_workloads;
    let requirements = derive_eip_requirements(workloads, &cfg.environment.mgmt_cluster)?;
    if requirements.is_empty() {
        bail!(
            "no EIP requirements could be derived; \
             configure workload clusters in bootstrap.toml"
        );
    }
    let mut errors: Vec<String> = Vec::new();

    for (region, plan) in &requirements {
        let required = plan.required;
        let limit_raw = capture(
            "aws",
            &[
                "service-quotas",
                "get-service-quota",
                "--service-code",
                "ec2",
                "--quota-code",
                "L-0263D0A3",
                "--region",
                region,
                "--query",
                "Quota.Value",
                "--output",
                "text",
            ],
        )
        .await
        .with_context(|| {
            format!(
                "failed to query EC2 EIP quota in {region}; check AWS credentials and permissions"
            )
        })?;
        let limit: u32 = limit_raw
            .trim()
            .parse::<f64>()
            .with_context(|| format!("unexpected EIP quota value '{limit_raw}' in {region}"))?
            as u32;

        let allocated_raw = capture(
            "aws",
            &[
                "ec2",
                "describe-addresses",
                "--region",
                region,
                "--filters",
                "Name=domain,Values=vpc",
                "--query",
                "length(Addresses)",
                "--output",
                "text",
            ],
        )
        .await
        .with_context(|| format!("failed to list EC2 EIPs in {region}"))?;
        let allocated: u32 = allocated_raw.trim().parse::<f64>().with_context(|| {
            format!("unexpected EIP allocation count '{allocated_raw}' in {region}")
        })? as u32;

        let mut owned: u32 = 0;
        for cluster in &plan.clusters {
            let filter = format!("Name=tag:{},Values=owned", teardown::capa_tag_key(cluster));
            let owned_raw = capture(
                "aws",
                &[
                    "ec2",
                    "describe-addresses",
                    "--region",
                    region,
                    "--filters",
                    &filter,
                    "--query",
                    "length(Addresses)",
                    "--output",
                    "text",
                ],
            )
            .await
            .with_context(|| format!("failed to list EC2 EIPs owned by {cluster} in {region}"))?;
            owned += owned_raw.trim().parse::<f64>().with_context(|| {
                format!("unexpected EIP owned count '{owned_raw}' for {cluster} in {region}")
            })? as u32;
        }

        let available = eip_available(limit, allocated, owned);
        match eip_shortfall(limit, allocated, owned, required) {
            Some(shortfall) => {
                let foreign = allocated.saturating_sub(owned);
                errors.push(format!(
                    "  {region}: {available} available to krops, {required} needed \
                     ({shortfall} short; limit {limit}, {allocated} allocated, {owned} owned by krops)\n    \
                     Request: aws service-quotas request-service-quota-increase \\\n      \
                     --service-code ec2 --quota-code L-0263D0A3 \\\n      \
                     --desired-value {} --region {region}",
                    foreign + required
                ));
            }
            None => println!(
                ">>> EIP quota in {region}: {available} available to krops \
                 (need {required}, {owned} already owned) - OK"
            ),
        }
    }

    if !errors.is_empty() {
        bail!("insufficient EC2 Elastic IP quota:\n{}", errors.join("\n"));
    }
    Ok(())
}

// ── Steps ─────────────────────────────────────────────────────────────────────

pub(crate) async fn select_toolbox_kind_kubeconfig(cfg: &Config) -> Result<()> {
    let kubeconfig = std::env::var("KUBECONFIG").ok();
    let Some(path) = toolbox_kubeconfig_path(cfg.toolbox, kubeconfig.as_deref())? else {
        return Ok(());
    };
    let args = internal_kind_kubeconfig_args(&cfg.repo.bootstrap.kind_cluster);
    let contents = capture("kind", &args).await?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("failed to create {}", parent.display()))?;
    }
    std::fs::write(&path, contents)
        .with_context(|| format!("failed to write {}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .with_context(|| format!("failed to chmod 600 {}", path.display()))?;
    }
    Ok(())
}

/// Ensure the kind 'mgmt' cluster exists and is healthy.
///
/// Default: reuse an existing cluster after validating that its context is
/// reachable and all nodes go Ready; this makes reruns non-destructive and
/// lets a partially failed bootstrap resume. With --recreate (or when no
/// cluster exists) the cluster is (re)built from the rendered config.
async fn ensure_kind_cluster(cfg: &Config, engine: &str, engine_sock: &str) -> Result<()> {
    let kind_cluster = &cfg.repo.bootstrap.kind_cluster;
    let kind_context = &cfg.repo.bootstrap.kind_context;
    let clusters = capture_lossy("kind", &["get", "clusters"]).await;
    let exists = clusters.lines().any(|l| l.trim() == kind_cluster);
    let node_ready_timeout = format!("--timeout={NODE_READY_TIMEOUT}");

    if exists && !cfg.recreate {
        println!(
            ">>> Reusing existing kind cluster '{kind_cluster}' (pass --recreate to replace it)..."
        );
        println!(">>> Validating existing cluster health...");
        {
            // Must precede the kubeconfig selection: the internal endpoint
            // only resolves once the toolbox is on the kind network.
            engine::toolbox_join_kind_network(cfg, engine).await?;
            select_toolbox_kind_kubeconfig(cfg).await?;
        }
        if !run_quiet("kubectl", &["config", "use-context", kind_context]).await {
            // kind get clusters can report mgmt while the kubeconfig lacks
            // the context (interrupted first create, pruned kubeconfig).
            // Recover it instead of demanding a destructive --recreate.
            println!(">>> Context '{kind_context}' missing from kubeconfig; exporting it...");
            if !run_quiet("kind", &["export", "kubeconfig", "--name", kind_cluster]).await {
                bail!(
                    "failed to export kubeconfig for existing cluster '{kind_cluster}'; rerun with --recreate to replace it"
                );
            }
        }
        if run_quiet("kubectl", &["config", "use-context", kind_context]).await
            && run_quiet(
                "kubectl",
                &[
                    "wait",
                    "--for=condition=Ready",
                    "node",
                    "--all",
                    &node_ready_timeout,
                ],
            )
            .await
        {
            println!(">>> Existing cluster '{kind_cluster}' is healthy; continuing.");
            return Ok(());
        }
        bail!(
            "existing kind cluster '{kind_cluster}' is not healthy (context unreachable or nodes not Ready within {NODE_READY_TIMEOUT}); rerun with --recreate to replace it"
        );
    }

    if exists {
        println!(">>> Cluster '{kind_cluster}' exists and --recreate was given – recreating...");
        engine::toolbox_leave_kind_network(cfg, engine).await;
        run("kind", &["delete", "cluster", "--name", kind_cluster]).await?;
    }

    println!(">>> Creating kind cluster '{kind_cluster}'...");
    // Mount the host's container engine socket into the kind node at the
    // standard Docker socket path so in-cluster components can reach a
    // Docker-compatible API whether the backend is Docker or Podman.
    let kind_config = render_kind_config(
        cfg.is_local(),
        cfg.registry_port,
        engine_sock,
        &cfg.repo.bootstrap.registry_name,
    );
    run_with_stdin(
        "kind",
        &["create", "cluster", "--name", kind_cluster, "--config", "-"],
        &kind_config,
    )
    .await?;
    engine::toolbox_join_kind_network(cfg, engine).await?;
    select_toolbox_kind_kubeconfig(cfg).await?;

    println!(">>> Waiting for cluster node to be ready...");
    // Explicitly switch kubectl to use the kind cluster context.
    run("kubectl", &["config", "use-context", kind_context]).await?;
    run(
        "kubectl",
        &[
            "wait",
            "--for=condition=Ready",
            "node",
            "--all",
            &node_ready_timeout,
        ],
    )
    .await?;
    Ok(())
}

async fn bootstrap_local_registry(
    cfg: &Config,
    engine: &str,
    http: &reqwest::Client,
) -> Result<()> {
    println!(">>> Bootstrapping local container registry...");
    let port = cfg.registry_port;
    let registry_name = &cfg.repo.bootstrap.registry_name;
    let name_filter = format!("name=^{registry_name}$");

    let exists = capture_lossy(
        engine,
        &[
            "ps",
            "-a",
            "--filter",
            &name_filter,
            "--format",
            "{{.Names}}",
        ],
    )
    .await
    .lines()
    .any(|l| l.trim() == registry_name.as_str());

    if !exists {
        println!("    Creating registry container '{registry_name}'...");
        let publish = format!("127.0.0.1:{port}:5000");
        capture(
            engine,
            &[
                "run",
                "-d",
                "--name",
                registry_name,
                "--network",
                "kind",
                "-p",
                &publish,
                "registry:2",
            ],
        )
        .await?;
        println!("    Registry created and running: {registry_name}:5000");
    } else {
        // Name match alone proves nothing about configuration. Verify the
        // host-port binding and the kind-network attachment; a stale
        // container bound elsewhere or detached from the network answers
        // the host readiness check and then fails in-cluster.
        let port_matches = extract_workload_port(
            &capture_lossy(engine, &["port", registry_name, "5000/tcp"]).await,
        )
        .is_some_and(|p| p.parse::<u16>().is_ok_and(|p| p == port));
        let in_kind_network = capture_lossy(
            engine,
            &[
                "inspect",
                registry_name,
                "--format",
                "{{json .NetworkSettings.Networks}}",
            ],
        )
        .await
        .contains("\"kind\"");
        if !port_matches || !in_kind_network {
            println!("    Existing registry misconfigured (port or network); recreating...");
            run(engine, &["rm", "-f", registry_name]).await?;
            let publish = format!("127.0.0.1:{port}:5000");
            capture(
                engine,
                &[
                    "run",
                    "-d",
                    "--name",
                    registry_name,
                    "--network",
                    "kind",
                    "-p",
                    &publish,
                    "registry:2",
                ],
            )
            .await?;
            println!("    Registry recreated: {registry_name}:5000");
        } else {
            let running = capture_lossy(
                engine,
                &["ps", "--filter", &name_filter, "--format", "{{.Names}}"],
            )
            .await
            .lines()
            .any(|l| l.trim() == registry_name.as_str());
            if !running {
                println!("    Restarting stopped registry...");
                capture(engine, &["start", registry_name]).await?;
                println!("    Registry restarted: {registry_name}:5000");
            } else {
                println!("    Registry already running: {registry_name}:5000");
            }
        }
    }

    let (registry_host, probe_port) = cfg.registry_endpoint();
    let registry_url = format!("http://{registry_host}:{probe_port}/v2/");
    println!(">>> Waiting for local registry API at {registry_host}:{probe_port}...");
    // Parity with the script's `curl --fail --retry N --retry-connrefused
    // --retry-delay 1`: one initial attempt plus N retries 1s apart; any
    // response below 400 succeeds (curl --fail's threshold); connection
    // errors (--retry-connrefused) and curl's documented transient
    // statuses (408, 429, 500, 502, 503, 504) are retried; any other
    // answer fails immediately instead of burning the retry budget.
    // The probe address is where THIS process reaches the registry: the
    // kind-network name inside the toolbox, the published port on the host.
    let mut ready = false;
    for attempt in 0..=cfg.registry_ready_retries {
        if let Ok(resp) = http.get(&registry_url).send().await {
            let status = resp.status().as_u16();
            if status < 400 {
                ready = true;
                break;
            }
            if !CURL_TRANSIENT_STATUSES.contains(&status) {
                bail!("local registry at {registry_url} returned {status}; not retrying");
            }
        }
        // Connection errors fall through and are retried (--retry-connrefused).
        if attempt < cfg.registry_ready_retries {
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    }
    if !ready {
        bail!("local registry did not become ready at {registry_url}");
    }

    // Tell the cluster about the local registry (apply = rerun-safe).
    kubectl_apply(
        None,
        &json!({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "local-registry-config",
                "namespace": "kube-system",
            },
            "data": { "registry-url": format!("{registry_name}:5000") },
        }),
    )
    .await?;

    println!(">>> Publishing initial OCI artifact from the local Git checkout...");
    // Forward the resolved values: the mise oci-push task reads them from
    // its own environment, so they must not stop at this boundary.
    // REGISTRY_HOST reroutes the task's own registry probe and push from
    // the published localhost port to the kind-network name (toolbox runs).
    let (registry_host, oci_port) = cfg.registry_endpoint();
    let status = Command::new("mise")
        .args(["-E", &cfg.profile, "run", "oci-push"])
        .env("REGISTRY_PORT", oci_port.to_string())
        .env("REGISTRY_HOST", &registry_host)
        .env("OCI_REPOSITORY", &cfg.oci_repository)
        .env("OCI_TAG", &cfg.oci_tag)
        .status()
        .await
        .with_context(|| "failed to spawn 'mise'")?;
    if !status.success() {
        bail!(
            "'mise -E {} run oci-push' failed with {status}",
            cfg.profile
        );
    }
    println!(
        ">>> Initial OCI artifact is available at oci://localhost:{port}/{repo}:{tag}",
        repo = cfg.oci_repository,
        tag = cfg.oci_tag
    );
    Ok(())
}

async fn install_flux_operator(
    repo: &BootstrapConfig,
    registry_config: &Path,
    kubeconfig: Option<&str>,
) -> Result<()> {
    println!(">>> Installing Flux Operator...");
    let cfg = registry_config.to_string_lossy().into_owned();
    let flux_ns = &repo.bootstrap.flux_namespace;
    let chart_version = repo
        .charts
        .get("flux-operator")
        .context("charts.flux-operator missing from bootstrap.toml")?;
    // upgrade --install (script used install): rerun-safe after a partial failure.
    // --version pins the chart so reruns cannot resolve a different release
    // (the chart pin lives in bootstrap.toml [charts], kept in sync with
    // the HelmRelease in Git by the cross-check and Renovate).
    let mut args: Vec<String> = vec![
        "upgrade".into(),
        "--install".into(),
        "flux-operator".into(),
        "oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator".into(),
        "--version".into(),
        chart_version.into(),
        "--namespace".into(),
        flux_ns.into(),
        "--create-namespace".into(),
        "--wait".into(),
        "--timeout".into(),
        "10m".into(),
        "--registry-config".into(),
        cfg,
    ];
    if let Some(kc) = kubeconfig {
        args.extend(["--kubeconfig".into(), kc.to_string()]);
    }
    let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
    run("helm", &arg_refs).await
}

async fn create_github_secrets(
    repo: &BootstrapConfig,
    github: &GithubContext,
    kubeconfig: Option<&str>,
) -> Result<()> {
    // Both secrets are applied as manifests on stdin: idempotent on rerun,
    // and no secret material ever appears on argv or in error messages.
    let flux_ns = &repo.bootstrap.flux_namespace;
    let pat_secret = &repo.bootstrap.github_pat_secret;
    let sops_secret = &repo.bootstrap.sops_age_secret;

    // Basic-auth secret consumed by Flux's source-controller to clone the repo.
    println!(">>> Creating GitHub PAT credentials secret in {flux_ns}...");
    kubectl_apply(
        kubeconfig,
        &json!({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": { "name": pat_secret, "namespace": flux_ns },
            "type": "Opaque",
            "stringData": {
                "username": github.github_user,
                "password": github.github_token,
            },
        }),
    )
    .await?;

    // Flux's kustomize-controller uses this key to decrypt *.sops.yaml
    // manifests during reconciliation. Flux scans the Secret for keys matching
    // `keys.<public-key>.agekey`.
    println!(">>> Creating sops-age decryption secret in {flux_ns}...");
    // Remove any existing sops-age secret to avoid stale keys from previous
    // bootstrap runs (apply alone would merge old key entries).
    run(
        "kubectl",
        &kubectl_cmd(
            kubeconfig,
            &[
                "delete",
                "secret",
                sops_secret,
                "-n",
                flux_ns,
                "--ignore-not-found",
            ],
        ),
    )
    .await?;
    kubectl_apply(
        kubeconfig,
        &json!({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": { "name": sops_secret, "namespace": flux_ns },
            "type": "Opaque",
            "stringData": {
                format!("keys.{}.agekey", github.age_pubkey): github.age_key_content,
            },
        }),
    )
    .await
}

async fn install_flux_instance(
    cfg: &Config,
    github: Option<&GithubContext>,
    registry_config: &Path,
    kubeconfig: Option<&str>,
) -> Result<bool> {
    println!(">>> Installing FluxInstance via Helm...");
    let mut controllers_ready = true;
    let registry_cfg = registry_config.to_string_lossy().into_owned();
    let flux_ns = &cfg.repo.bootstrap.flux_namespace;
    let chart_version = cfg
        .repo
        .charts
        .get("flux-operator")
        .context("charts.flux-operator missing from bootstrap.toml")?;
    let mut args: Vec<String> = vec![
        "upgrade".into(),
        "--install".into(),
        "flux".into(),
        "oci://ghcr.io/controlplaneio-fluxcd/charts/flux-instance".into(),
        "--version".into(),
        chart_version.into(),
        "--namespace".into(),
        flux_ns.into(),
        // Helm 4's watcher strategy treats the FluxInstance's transient
        // InProgress condition as a terminal failure. Use the legacy
        // chart-resource wait here, then wait explicitly for the
        // operator-owned Ready condition below.
        "--wait=legacy".into(),
        "--timeout".into(),
        "10m".into(),
        "--set".into(),
        "instance.cluster.type=kubernetes".into(),
        "--set".into(),
        "instance.cluster.size=small".into(),
        "--set".into(),
        "instance.cluster.multitenant=false".into(),
        "--set".into(),
        "instance.cluster.networkPolicy=true".into(),
        "--set".into(),
        "instance.cluster.domain=cluster.local".into(),
        "--registry-config".into(),
        registry_cfg,
    ];

    if let Some(kc) = kubeconfig {
        args.extend(["--kubeconfig".into(), kc.to_string()]);
    }

    match github {
        Some(github) => {
            args.extend([
                "--set".into(),
                "instance.sync.kind=GitRepository".into(),
                "--set".into(),
                format!("instance.sync.url={}", github.git_repo_url),
                "--set".into(),
                format!(
                    "instance.sync.ref=refs/heads/{}",
                    cfg.repo.bootstrap.git_branch
                ),
                "--set".into(),
                format!("instance.sync.path={}", cfg.environment.sync_path),
                "--set".into(),
                format!(
                    "instance.sync.pullSecret={}",
                    cfg.repo.bootstrap.github_pat_secret
                ),
            ]);
        }
        None => {
            args.extend([
                "--set".into(),
                "instance.sync.kind=OCIRepository".into(),
                "--set".into(),
                format!(
                    "instance.sync.url=oci://{}:5000/{}",
                    cfg.repo.bootstrap.registry_name,
                    cfg.oci_repository
                ),
                "--set".into(),
                format!("instance.sync.ref={}", cfg.oci_tag),
                "--set".into(),
                format!("instance.sync.path={}", cfg.environment.sync_path),
                "--set-json".into(),
                r#"instance.kustomize.patches=[{"patch":"- op: add\n  path: /spec/insecure\n  value: true","target":{"kind":"OCIRepository"}}]"#.into(),
            ]);
        }
    }

    let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
    run("helm", &arg_refs).await?;

    println!(">>> Waiting for FluxInstance reconciliation to complete...");
    run(
        "kubectl",
        &kubectl_cmd(
            kubeconfig,
            &[
                "wait",
                "fluxinstance/flux",
                "--namespace",
                flux_ns,
                "--for=condition=Ready",
                "--timeout=10m",
            ],
        ),
    )
    .await?;

    // Verify the Flux controllers are running before declaring success.
    // The script tolerated this wait failing (`|| true`); keep the
    // tolerance but make the failure visible and stop short of the plain
    // completion message, which would otherwise report success over
    // ImagePullBackOff'd controllers.
    println!(">>> Waiting for Flux controllers to be ready...");
    if run(
        "kubectl",
        &kubectl_cmd(
            kubeconfig,
            &[
                "wait",
                "--namespace",
                flux_ns,
                "--for=condition=ready",
                "pod",
                "--selector=app.kubernetes.io/part-of=flux",
                "--timeout=90s",
            ],
        ),
    )
    .await
    .is_err()
    {
        eprintln!("WARNING: not all Flux controllers became ready within 90s:");
        let _ = run(
            "kubectl",
            &kubectl_cmd(kubeconfig, &["get", "pods", "--namespace", flux_ns]),
        )
        .await;
        controllers_ready = false;
    }
    Ok(controllers_ready)
}

/// Poll `kubectl <args>` until it succeeds, up to `attempts` tries 2s apart.
async fn wait_for_resource(args: &[&str], attempts: u32) -> bool {
    for _ in 0..attempts {
        if run_quiet("kubectl", args).await {
            return true;
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    false
}

/// Poll `probe` immediately, then every `interval_s` after a failure, until
/// it reports success or the `timeout_s` budget is spent. One final probe
/// runs past the budget, mirroring the script's until-loop which tests the
/// condition once more before declaring failure.
async fn poll_until<F, Fut>(timeout_s: u64, interval_s: u64, mut probe: F) -> bool
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    let max_attempts = timeout_s.checked_div(interval_s).unwrap_or(1).max(1);
    for _ in 0..max_attempts {
        if probe().await {
            return true;
        }
        tokio::time::sleep(Duration::from_secs(interval_s)).await;
    }
    probe().await
}

async fn watch_local_reconciliation(cfg: &Config, engine: &str) -> Result<()> {
    println!();
    println!(">>> Step 5: Flux reconciliation progress");

    // The final Kustomization is created by the OCI root, so wait for it to
    // appear before asking kubectl to wait for readiness.
    if !wait_for_resource(
        &[
            "get",
            "kustomization",
            "flux-apps",
            "--namespace",
            &cfg.repo.bootstrap.flux_namespace,
        ],
        60,
    )
    .await
    {
        eprintln!("ERROR: flux-apps Kustomization was not created within 2 minutes");
        let _ = run("flux", &["get", "kustomizations"]).await;
        bail!("flux-apps Kustomization missing");
    }

    println!(">>> Waiting until the local workload cluster and Flux addons are ready...");
    let timeout_arg = format!("--timeout={}", cfg.local_reconcile_timeout);
    if run(
        "kubectl",
        &[
            "wait",
            "kustomization/flux-apps",
            "--namespace",
            &cfg.repo.bootstrap.flux_namespace,
            "--for=condition=Ready",
            &timeout_arg,
        ],
    )
    .await
    .is_err()
    {
        eprintln!(
            "ERROR: local-host reconciliation did not complete within {}",
            cfg.local_reconcile_timeout
        );
        let _ = run("flux", &["get", "kustomizations"]).await;
        bail!("local-host reconciliation timed out");
    }

    println!();
    println!(">>> Workload cluster Flux reconciliation errors");
    let workload_kubeconfig =
        tempfile::NamedTempFile::new().context("failed to create temp kubeconfig")?;
    let kubeconfig_path = workload_kubeconfig.path().to_string_lossy().into_owned();

    let kubeconfig_content =
        capture("clusterctl", &["get", "kubeconfig", "local-workload"]).await?;
    let recorded_endpoint = capd_recorded_endpoint(&kubeconfig_content);
    std::fs::write(workload_kubeconfig.path(), kubeconfig_content)?;

    // Host runs rewrite the endpoint to the published localhost port; the
    // toolbox is on the kind network, where the CAPD-recorded endpoint
    // already resolves, so the kubeconfig is written as-is.
    let server = if cfg.should_rewrite_capd_endpoint() {
        let port_output = capture(engine, &["port", "local-workload-lb", "6443/tcp"]).await?;
        let Some(workload_port) = extract_workload_port(&port_output) else {
            bail!("cannot determine the local-workload API server port");
        };
        format!("https://127.0.0.1:{workload_port}")
    } else {
        // Reuse the endpoint recorded by CAPD (kind-network address).
        recorded_endpoint
            .context("cannot read the local-workload endpoint from the CAPD kubeconfig")?
    };
    let kubeconfig_flag = format!("--kubeconfig={kubeconfig_path}");
    capture(
        "kubectl",
        &[
            "config",
            "set-cluster",
            "local-workload",
            &format!("--server={server}"),
            &kubeconfig_flag,
        ],
    )
    .await?;

    if !wait_for_resource(
        &[
            "--kubeconfig",
            &kubeconfig_path,
            "get",
            "kustomization",
            "flux-system",
            "--namespace",
            "flux-system",
        ],
        60,
    )
    .await
    {
        eprintln!("ERROR: workload Flux Kustomization was not created within 2 minutes");
        let _ = run(
            "kubectl",
            &[
                "--kubeconfig",
                &kubeconfig_path,
                "get",
                "pods",
                "--namespace",
                "flux-system",
            ],
        )
        .await;
        bail!("workload Flux Kustomization missing");
    }

    println!(">>> Waiting for workload Flux controllers to be ready...");
    run(
        "kubectl",
        &[
            "--kubeconfig",
            &kubeconfig_path,
            "wait",
            "pod",
            "--namespace",
            "flux-system",
            "--selector=app.kubernetes.io/part-of=flux",
            "--for=condition=Ready",
            &timeout_arg,
        ],
    )
    .await?;

    // Stream workload Flux errors in the bootstrap terminal while we wait.
    let flux_logs = Command::new("flux")
        .args([
            "logs",
            "--kubeconfig",
            &kubeconfig_path,
            "--all-namespaces",
            "--follow",
            "--level=error",
            "--since=10m",
        ])
        .spawn()
        .context("failed to spawn 'flux logs'")?;
    let _log_guard = ChildGuard(flux_logs);

    if run(
        "kubectl",
        &[
            "--kubeconfig",
            &kubeconfig_path,
            "wait",
            "kustomization/flux-system",
            "--namespace",
            "flux-system",
            "--for=condition=Ready",
            &timeout_arg,
        ],
    )
    .await
    .is_err()
    {
        eprintln!(
            "ERROR: workload reconciliation did not complete within {}",
            cfg.local_reconcile_timeout
        );
        let _ = run(
            "flux",
            &[
                "get",
                "kustomizations",
                "--kubeconfig",
                &kubeconfig_path,
                "--all-namespaces",
            ],
        )
        .await;
        bail!("workload reconciliation timed out");
    }
    // _log_guard drops here: the flux logs follower is killed and the temp
    // kubeconfig is removed when workload_kubeconfig drops.
    Ok(())
}

// ── Pivot (port of pivot.sh; issue #95) ───────────────────────────────────────
//
// The bootstrap's default exit moves the CAPI management inventory from the
// kind bootstrap cluster into the self-managed management cluster, then
// deletes kind. Phases mirror pivot.sh:
//   0 preflight + wait for the Flux-created management Cluster definition
//   (issue #348), 1 wait for the management cluster, 2 export its kubeconfig
//   and wait for nodes (nodeless-tolerant, issue #349), 3 install CAPI in
//   the target, 4 suspend Flux in kind and move, 5 seed Flux on the target,
//   6 delete the bootstrap cluster.
// The move stays re-runnable: objects are deleted from the source only after
// they were created on the target, so kind stays authoritative until Phase 6.

/// One readiness probe of the local registry /v2/ endpoint (script: `curl
/// --fail`): any response below 400 counts as serving. The toolbox reaches
/// the registry by network name instead of the published localhost port.
async fn registry_reachable(cfg: &Config, http: &reqwest::Client) -> bool {
    let (host, port) = cfg.registry_endpoint();
    http.get(format!("http://{host}:{port}/v2/"))
        .send()
        .await
        .map(|r| r.status().as_u16() < 400)
        .unwrap_or(false)
}

/// Phase 0: the kind bootstrap cluster must be the move SOURCE.
async fn pivot_check_context(cfg: &Config) -> Result<()> {
    let current = capture_lossy("kubectl", &["config", "current-context"])
        .await
        .trim()
        .to_string();
    if current != cfg.bootstrap_kubecontext {
        eprintln!(
            "ERROR: current kubectl context is '{current}', expected '{}'.",
            cfg.bootstrap_kubecontext
        );
        eprintln!("       The kind bootstrap cluster must be the move SOURCE.");
        eprintln!(
            "       Fix with: kubectl config use-context {}",
            cfg.bootstrap_kubecontext
        );
        bail!(
            "current kubectl context is '{current}', expected '{}'",
            cfg.bootstrap_kubecontext
        );
    }
    Ok(())
}

/// Phase 0 (issue #348): poll for the management Cluster object with the
/// Phase 1 budget instead of failing fast. Creating the definition is Flux's
/// job after the bootstrap handoff (the CAPA HelmRelease, providers,
/// identity, and clusters overlay), a multi-minute chain on a clean first
/// run. `diagnose` runs on timeout to surface the Kustomization conditions,
/// so a stuck Flux is distinguishable from one still reconciling.
async fn wait_for_cluster_definition<F, D, Fut, DFut>(
    mgmt_cluster: &str,
    timeout: &str,
    poll_interval: u64,
    probe: F,
    diagnose: D,
) -> Result<()>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
    D: FnOnce() -> DFut,
    DFut: std::future::Future<Output = ()>,
{
    let timeout_s = parse_duration_seconds(timeout)?;
    if poll_until(timeout_s, poll_interval, probe).await {
        return Ok(());
    }
    eprintln!("ERROR: Cluster '{mgmt_cluster}' was not created within {timeout}.");
    diagnose().await;
    eprintln!("       Flux creates the management cluster definition after the bootstrap");
    eprintln!("       handoff; a failed Kustomization above is why the Cluster is missing.");
    eprintln!("       Re-run the same command once Flux has reconciled: the chain is rerun-safe.");
    bail!("Cluster '{mgmt_cluster}' was not created within {timeout}");
}

/// The Phase 0 wait against the kind bootstrap cluster (current kubectl
/// context); on timeout lists the Flux Kustomizations as the diagnostic.
async fn wait_for_mgmt_cluster_definition(cfg: &Config) -> Result<()> {
    let mgmt_cluster = cfg.environment.mgmt_cluster.clone();
    let mgmt_ns = cfg.repo.bootstrap.mgmt_namespace.clone();
    let flux_ns = cfg.repo.bootstrap.flux_namespace.clone();
    println!(
        ">>> Waiting for the management cluster definition (timeout: {})...",
        cfg.mgmt_ready_timeout
    );
    let probe_cluster = mgmt_cluster.clone();
    wait_for_cluster_definition(
        &mgmt_cluster,
        &cfg.mgmt_ready_timeout,
        cfg.mgmt_poll_interval,
        move || {
            let (cluster, ns) = (probe_cluster.clone(), mgmt_ns.clone());
            async move { run_quiet("kubectl", &["get", "cluster", &cluster, "-n", &ns]).await }
        },
        move || async move {
            let _ = run("kubectl", &["get", "kustomizations", "-n", &flux_ns]).await;
        },
    )
    .await
}

/// Phase 1: wait for the management cluster kubeconfig secret.
///
/// CAPI Clusters do not expose a uniform Ready condition across providers,
/// so readiness = the kubeconfig secret exists (the control plane has an
/// endpoint). Poll rather than kubectl-wait so a not-yet-created secret is
/// waited on instead of erroring immediately. Node readiness is verified in
/// Phase 2 against the exported kubeconfig.
async fn pivot_wait_for_management_cluster(cfg: &Config, mgmt_cluster: &str) -> Result<()> {
    let mgmt_ns = &cfg.repo.bootstrap.mgmt_namespace;
    println!(
        ">>> Waiting for the management cluster kubeconfig (timeout: {})...",
        cfg.mgmt_ready_timeout
    );
    let secret = format!("secret/{mgmt_cluster}-kubeconfig");
    let timeout_s = parse_duration_seconds(&cfg.mgmt_ready_timeout)?;
    let max_attempts = (timeout_s / cfg.mgmt_poll_interval).max(1);
    let mut found = false;
    for _ in 0..max_attempts {
        if run_quiet("kubectl", &["get", &secret, "-n", mgmt_ns]).await {
            found = true;
            break;
        }
        tokio::time::sleep(Duration::from_secs(cfg.mgmt_poll_interval)).await;
    }
    if !found {
        // One final probe past the budget, mirroring the script's until-loop
        // which tests the condition before declaring failure.
        found = run_quiet("kubectl", &["get", &secret, "-n", mgmt_ns]).await;
    }
    if !found {
        eprintln!(
            "ERROR: management cluster kubeconfig not available within {}",
            cfg.mgmt_ready_timeout
        );
        let _ = run(
            "kubectl",
            &["describe", "cluster", mgmt_cluster, "-n", mgmt_ns],
        )
        .await;
        bail!(
            "management cluster kubeconfig not available within {}",
            cfg.mgmt_ready_timeout
        );
    }
    let _ = run(
        "clusterctl",
        &["describe", "cluster", mgmt_cluster, "-n", mgmt_ns],
    )
    .await;
    Ok(())
}

/// Node readiness of the pivot target, parsed from `kubectl get nodes -o
/// json` (issue #349). Success = at least one node registered AND every
/// registered node Ready.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NodesReadiness {
    /// Zero nodes registered: `kubectl wait node --all` errors "no matching
    /// resources found" here, which is why Phase 2 polls instead.
    Nodeless,
    /// Nodes registered but at least one not Ready, or unreadable output
    /// (kubectl failed against a briefly unreachable API): keep waiting.
    Pending,
    Ready,
}

fn nodes_readiness(get_nodes_json: &str) -> NodesReadiness {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(get_nodes_json) else {
        return NodesReadiness::Pending;
    };
    let Some(items) = value.get("items").and_then(serde_json::Value::as_array) else {
        return NodesReadiness::Pending;
    };
    if items.is_empty() {
        return NodesReadiness::Nodeless;
    }
    let all_ready = items.iter().all(|item| {
        item.get("status")
            .and_then(|status| status.get("conditions"))
            .and_then(serde_json::Value::as_array)
            .is_some_and(|conditions| {
                conditions.iter().any(|condition| {
                    condition.get("type").and_then(serde_json::Value::as_str) == Some("Ready")
                        && condition.get("status").and_then(serde_json::Value::as_str)
                            == Some("True")
                })
            })
    });
    if all_ready {
        NodesReadiness::Ready
    } else {
        NodesReadiness::Pending
    }
}

/// Phase 2 node wait (issue #349): poll node readiness with the same budget
/// the bare `kubectl wait node --all --timeout=15m` had, tolerating a
/// nodeless target (on EKS the CAPA MachinePool registers nodes 1-2 min
/// after the control plane goes ACTIVE). `diagnose` runs on timeout.
async fn wait_for_nodes_ready<F, D, Fut, DFut>(
    timeout: &str,
    poll_interval: u64,
    probe: F,
    diagnose: D,
) -> Result<()>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = NodesReadiness>,
    D: FnOnce() -> DFut,
    DFut: std::future::Future<Output = ()>,
{
    let timeout_s = parse_duration_seconds(timeout)?;
    let mut probe = probe;
    let ready = poll_until(timeout_s, poll_interval, move || {
        let future = probe();
        async move { matches!(future.await, NodesReadiness::Ready) }
    })
    .await;
    if ready {
        return Ok(());
    }
    eprintln!("ERROR: management cluster nodes were not all Ready within {timeout}.");
    diagnose().await;
    eprintln!("       The node pool registers nodes after the control plane goes ACTIVE;");
    eprintln!("       re-run the same command once nodes register: the pivot is rerun-safe.");
    bail!("management cluster nodes were not all Ready within {timeout}");
}

/// Phase 2: export the target kubeconfig (chmod 600), rewrite the CAPD
/// endpoint to localhost, rename the context, wait for nodes Ready.
async fn pivot_export_kubeconfig(cfg: &Config, engine: &str, mgmt_cluster: &str) -> Result<()> {
    let path = &cfg.mgmt_kubeconfig;
    println!(
        ">>> Exporting management-cluster kubeconfig to {}...",
        path.display()
    );
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("failed to create {}", parent.display()))?;
        }
    }
    let kubeconfig = capture(
        "clusterctl",
        &[
            "get",
            "kubeconfig",
            mgmt_cluster,
            "-n",
            &cfg.repo.bootstrap.mgmt_namespace,
        ],
    )
    .await?;
    std::fs::write(path, &kubeconfig)
        .with_context(|| format!("failed to write {}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
            .with_context(|| format!("failed to chmod 600 {}", path.display()))?;
    }
    let kc = path.to_string_lossy().into_owned();

    if cfg.should_rewrite_capd_endpoint() {
        // CAPD records the load balancer's container-network IP, which is
        // not routable from macOS. Point the kubeconfig at the port
        // published on localhost instead (same rewrite as the workload
        // kubeconfigs). Toolbox runs stay on the kind network where the
        // recorded endpoint already resolves, so no rewrite.
        let lb = format!("{mgmt_cluster}-lb");
        let port_output = capture(engine, &["port", &lb, "6443/tcp"]).await?;
        let Some(port) = extract_workload_port(&port_output) else {
            bail!("cannot determine the {mgmt_cluster} API server port");
        };
        let server = format!("--server=https://127.0.0.1:{port}");
        run(
            "kubectl",
            &kubectl_cmd(Some(&kc), &["config", "set-cluster", mgmt_cluster, &server]),
        )
        .await?;
    }

    let current = capture(
        "kubectl",
        &kubectl_cmd(Some(&kc), &["config", "current-context"]),
    )
    .await?
    .trim()
    .to_string();
    run(
        "kubectl",
        &kubectl_cmd(
            Some(&kc),
            &[
                "config",
                "rename-context",
                &current,
                &cfg.repo.bootstrap.mgmt_context,
            ],
        ),
    )
    .await?;

    // `kubectl wait node --all` errors "no matching resources found" while
    // the target has zero nodes, and on EKS the CAPA MachinePool registers
    // nodes 1-2 min after the control plane goes ACTIVE (issue #349): poll
    // until at least one node exists AND every registered node is Ready.
    println!(
        ">>> Waiting for management-cluster nodes to be ready (timeout: {MGMT_NODE_READY_TIMEOUT})..."
    );
    let probe_kc = kc.clone();
    let diag_kc = kc.clone();
    let diag_cluster = mgmt_cluster.to_string();
    let diag_ns = cfg.repo.bootstrap.mgmt_namespace.clone();
    wait_for_nodes_ready(
        MGMT_NODE_READY_TIMEOUT,
        cfg.mgmt_poll_interval,
        move || {
            let kc = probe_kc.clone();
            async move {
                let output = capture_lossy(
                    "kubectl",
                    &kubectl_cmd(Some(&kc), &["get", "nodes", "-o", "json"]),
                )
                .await;
                nodes_readiness(&output)
            }
        },
        move || async move {
            let _ = run("kubectl", &kubectl_cmd(Some(&diag_kc), &["get", "nodes"])).await;
            let _ = run(
                "clusterctl",
                &["describe", "cluster", &diag_cluster, "-n", &diag_ns],
            )
            .await;
        },
    )
    .await?;
    let _ = run("kubectl", &kubectl_cmd(Some(&kc), &["get", "nodes"])).await;
    Ok(())
}

/// Phase 3: imperative but Git-identical target installs. The target has no
/// Flux yet, so the HelmReleases cannot be reconciled; after Phase 5 Flux
/// adopts these installs and provider CRs without drift (same charts, same
/// versions, same CRs).
async fn pivot_install_capi_in_target(
    cfg: &Config,
    registry_config: &Path,
    kc: &str,
) -> Result<()> {
    let cert_manager_version = cfg
        .repo
        .charts
        .get("cert-manager")
        .context("charts.cert-manager missing from bootstrap.toml")?;
    println!(">>> Installing cert-manager {cert_manager_version} in the target...");
    // upgrade --install (script used install): rerun-safe after a partial
    // failure, same intentional divergence as the #93 helm installs.
    // Values mirror mgmt/<env>/infrastructure/cert-manager/helmrelease.yaml.
    let registry_cfg = registry_config.to_string_lossy().into_owned();
    run(
        "helm",
        &[
            "upgrade",
            "--install",
            "cert-manager",
            "cert-manager",
            "--repo",
            "https://charts.jetstack.io",
            "--version",
            cert_manager_version,
            "--namespace",
            "cert-manager",
            "--create-namespace",
            "--wait",
            "--set",
            "crds.enabled=true",
            "--registry-config",
            &registry_cfg,
            "--kubeconfig",
            kc,
        ],
    )
    .await?;

    let capi_operator_version = cfg
        .repo
        .charts
        .get("capi-operator")
        .context("charts.capi-operator missing from bootstrap.toml")?;
    println!(">>> Installing CAPI operator {capi_operator_version} in the target...");
    // Values mirror mgmt/<env>/infrastructure/capi-operator/helmrelease.yaml.
    run(
        "helm",
        &[
            "upgrade",
            "--install",
            "capi-operator",
            "cluster-api-operator",
            "--repo",
            "https://kubernetes-sigs.github.io/cluster-api-operator",
            "--version",
            capi_operator_version,
            "--namespace",
            "capi-operator-system",
            "--create-namespace",
            "--wait",
            "--set",
            "cert-manager.enabled=false",
            "--registry-config",
            &registry_cfg,
            "--kubeconfig",
            kc,
        ],
    )
    .await?;

    println!(">>> Applying provider CRs in the target...");
    for manifest in &cfg.environment.provider_manifests {
        run(
            "kubectl",
            &kubectl_cmd(Some(kc), &["apply", "-f", manifest]),
        )
        .await?;
    }

    // Plain pivot manifests (issue #236): non-secret objects the moved
    // resources reference by name that clusterctl does not carry (the
    // workload-identity aso-credentials Secret). Applied pre-move for the
    // same reason as pivot-sops-secrets below, minus the decryption.
    // The Git file carries ${VAR} placeholders (Flux postBuild substitutes
    // them on the source side); the target has no Flux yet, so substitute
    // here from the ConfigMaps the bootstrap cluster's Flux reconciled
    // (azure-vars) before applying, and fail naming any variable that has
    // no source value.
    if !cfg.environment.pivot_manifests.is_empty() {
        println!(">>> Applying pivot manifests in the target...");
        let mut vars = flux_namespace_vars(&cfg.repo.bootstrap.flux_namespace).await?;
        // Environment overrides are applied on top of the ConfigMap data
        // (issue #72): the merge above is unordered across ConfigMaps, so a
        // value that differs between the bootstrap cluster and the target
        // (gcp: GCP_WIF_PROVIDER is `kind` in kind, `mgmt` afterwards) is
        // forced here rather than left to the unordered ConfigMap merge.
        for (key, value) in &cfg.environment.pivot_manifest_vars {
            vars.insert(key.clone(), value.clone());
        }
        for manifest in &cfg.environment.pivot_manifests {
            let raw = std::fs::read_to_string(manifest)
                .with_context(|| format!("failed to read pivot manifest '{manifest}'"))?;
            let substituted = substitute_manifest_vars(&raw, &vars);
            let missing = remaining_manifest_vars(&substituted);
            ensure!(
                missing.is_empty(),
                "pivot manifest '{manifest}' has unsubstituted placeholders (no ConfigMap data in {} provides them): {}",
                cfg.repo.bootstrap.flux_namespace,
                missing.join(", ")
            );
            run_with_stdin(
                "kubectl",
                &kubectl_cmd(Some(kc), &["apply", "-f", "-"]),
                &substituted,
            )
            .await?;
        }
    }

    if cfg.profile == "aws" {
        // CAPA credentials: the InfrastructureProvider above references the
        // aws-credentials secret (configSecret.name). On the bootstrap
        // cluster Flux decrypts aws-credentials.sops.yaml; here it is
        // created directly with the same shape. Do NOT pre-apply
        // mgmt/aws/infrastructure/aws-identity/: the
        // AWSClusterControllerIdentity carries a move hook and comes over
        // with the Phase 4 move. The credential travels in a stdin
        // manifest, never on argv.
        println!(">>> Creating CAPA credentials secret in capa-system...");
        let credentials = capture("mise", &["-E", &cfg.profile, "run", "aws-credentials"]).await?;
        kubectl_apply(
            Some(kc),
            &json!({
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": { "name": "aws-credentials", "namespace": "capa-system" },
                "type": "Opaque",
                "stringData": { "AWS_B64ENCODED_CREDENTIALS": credentials.trim_end() },
            }),
        )
        .await?;
    }

    // Config-driven pivot secrets (issue #71): decrypt each declared
    // *.sops.yaml with the operator's age key and apply it to the target
    // over stdin. On the bootstrap cluster Flux decrypts these; the target
    // has no Flux yet and the moved objects reference them by name.
    if !cfg.environment.pivot_sops_secrets.is_empty() {
        println!(">>> Applying pivot SOPS secrets in the target...");
        if std::env::var_os("SOPS_AGE_KEY_FILE").is_none() {
            std::env::set_var("SOPS_AGE_KEY_FILE", cfg.age_key_file.as_os_str());
        }
        for manifest in &cfg.environment.pivot_sops_secrets {
            let plaintext = capture(
                "sops",
                &[
                    "--decrypt",
                    "--input-type",
                    "yaml",
                    "--output-type",
                    "yaml",
                    manifest,
                ],
            )
            .await?;
            run_with_stdin(
                "kubectl",
                &kubectl_cmd(Some(kc), &["apply", "-f", "-"]),
                &plaintext,
            )
            .await?;
        }
    }

    println!(">>> Waiting for providers in the target...");
    let infra_ns = &cfg.environment.infra_provider_namespace;
    let infra_name = &cfg.environment.infra_provider_name;
    let infra_ref = format!("infrastructureprovider/{infra_name}");
    run(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &[
                "wait",
                "--for=condition=Ready",
                "coreprovider/cluster-api",
                "-n",
                "capi-system",
                "--timeout=15m",
            ],
        ),
    )
    .await?;
    run(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &[
                "wait",
                "--for=condition=Ready",
                "bootstrapprovider/kubeadm",
                "controlplaneprovider/kubeadm",
                "-n",
                "capi-system",
                "--timeout=15m",
            ],
        ),
    )
    .await?;
    run(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &[
                "wait",
                "--for=condition=Ready",
                &infra_ref,
                "-n",
                infra_ns,
                "--timeout=15m",
            ],
        ),
    )
    .await?;
    run(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &[
                "wait",
                "--for=condition=Ready",
                "addonprovider/helm",
                "-n",
                "caaph-system",
                "--timeout=15m",
            ],
        ),
    )
    .await?;

    // `clusterctl move` requires every source provider to exist in the
    // target at >= its source version. Same files + same catalog = same
    // versions; the listings below make the comparison visible in the log.
    println!(">>> Source providers:");
    let _ = run("kubectl", &["get", PROVIDER_KINDS, "-A"]).await;
    println!(">>> Target providers:");
    let _ = run(
        "kubectl",
        &kubectl_cmd(Some(kc), &["get", PROVIDER_KINDS, "-A"]),
    )
    .await;
    Ok(())
}

const PROVIDER_KINDS: &str =
    "coreproviders,bootstrapproviders,controlplaneproviders,infrastructureproviders,addonproviders";

/// Phase 4: suspend Flux in kind, run the move, unpause moved Clusters.
async fn pivot_suspend_and_move(cfg: &Config, kc: &str) -> Result<()> {
    // clusterctl move pauses Clusters on the source and deletes the moved
    // objects after creating them on the target. Flux-in-kind must not
    // reconcile mid-move (Git carries no spec.paused, so it would unpause
    // the Clusters and recreate deleted objects). kind is abandoned after
    // the pivot, so the suspension is never lifted there.
    let flux_ns = &cfg.repo.bootstrap.flux_namespace;
    println!(">>> Suspending Flux Kustomizations in the bootstrap cluster...");
    let ks_names = capture_lossy(
        "kubectl",
        &["get", "kustomizations", "-n", flux_ns, "-o", "name"],
    )
    .await;
    let patch = suspend_patch().to_string();
    for name in ks_names.lines().map(str::trim).filter(|l| !l.is_empty()) {
        run(
            "kubectl",
            &[
                "patch", name, "-n", flux_ns, "--type", "merge", "-p", &patch,
            ],
        )
        .await?;
    }

    println!(">>> Moving the CAPI inventory to the management cluster...");
    if run(
        "clusterctl",
        &[
            "move",
            "--to-kubeconfig",
            kc,
            "-n",
            &cfg.repo.bootstrap.mgmt_namespace,
        ],
    )
    .await
    .is_err()
    {
        eprintln!();
        eprintln!("ERROR: clusterctl move failed.");
        eprintln!("       The move is re-runnable: objects are deleted from the source only");
        eprintln!("       after they were created on the target, so kind stays authoritative.");
        eprintln!("       NEVER delete moved Cluster / AWSManaged* / MachinePool / Dev* objects");
        eprintln!("       on the target to work around a failure — the provider would");
        eprintln!("       deprovision the real infrastructure. See docs/operations.md");
        eprintln!("       'Pivot recovery'.");
        bail!("clusterctl move failed");
    }

    // Moved Clusters are created on the target with spec.paused=true (the
    // move pauses them); Git carries no paused field, so Flux would never
    // clear it. Flux is not seeded on the target yet, so unpausing here is
    // race-free.
    println!(">>> Unpausing moved Clusters on the target...");
    let patch = unpause_patch().to_string();
    let clusters = capture_lossy(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &[
                "get",
                "clusters",
                "-n",
                &cfg.repo.bootstrap.mgmt_namespace,
                "-o",
                "name",
            ],
        ),
    )
    .await;
    for name in clusters.lines().map(str::trim).filter(|l| !l.is_empty()) {
        // The script tolerates individual unpause failures (`|| true`).
        let _ = run_quiet(
            "kubectl",
            &kubectl_cmd(
                Some(kc),
                &[
                    "patch",
                    name,
                    "-n",
                    &cfg.repo.bootstrap.mgmt_namespace,
                    "--type",
                    "merge",
                    "-p",
                    &patch,
                ],
            ),
        )
        .await;
    }

    println!(">>> Clusters on the management cluster after the move:");
    run(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &["get", "clusters", "-n", &cfg.repo.bootstrap.mgmt_namespace],
        ),
    )
    .await?;

    // Move fallbacks ([environments.<name>.move-fallbacks]): objects that
    // normally come over with the inventory (they carry move hooks) but
    // are re-applied from the checkout if missing in the target.
    let mgmt_ns = &cfg.repo.bootstrap.mgmt_namespace;
    for fallback in &cfg.environment.move_fallbacks {
        if !run_quiet(
            "kubectl",
            &kubectl_cmd(
                Some(kc),
                &["get", &fallback.resource, &fallback.name, "-n", mgmt_ns],
            ),
        )
        .await
        {
            println!(
                ">>> {} '{}' missing after the move; applying from Git...",
                fallback.resource, fallback.name
            );
            run(
                "kubectl",
                &kubectl_cmd(Some(kc), &["apply", "-f", &fallback.manifest]),
            )
            .await?;
        }
    }
    Ok(())
}

/// Phase 5: seed Flux on the management cluster (seed_flux against the
/// target kubeconfig; oci-push first for local-host).
async fn pivot_seed_target(
    cfg: &Config,
    preflight: &Preflight,
    registry_config: &Path,
    kc: &str,
) -> Result<()> {
    // aws: syncs mgmt/aws from GitHub main. local-host: syncs
    // mgmt/local-host from the local OCI registry; the artifact must
    // contain the management manifests.
    if cfg.is_local() {
        println!(">>> Publishing the current checkout as the OCI artifact...");
        // Same endpoint forwarding as the initial publish: the mise
        // oci-push task probes REGISTRY_HOST:REGISTRY_PORT, which is the
        // kind-network name inside the toolbox and localhost on the host.
        let (registry_host, oci_port) = cfg.registry_endpoint();
        let status = Command::new("mise")
            .args(["-E", &cfg.profile, "run", "oci-push"])
            .env("REGISTRY_PORT", oci_port.to_string())
            .env("REGISTRY_HOST", &registry_host)
            .env("OCI_REPOSITORY", &cfg.oci_repository)
            .env("OCI_TAG", &cfg.oci_tag)
            .status()
            .await
            .context("failed to spawn 'mise'")?;
        if !status.success() {
            bail!(
                "'mise -E {} run oci-push' failed with {status}",
                cfg.profile
            );
        }
    }

    // seed_flux against the target: the same operator + secrets + instance
    // sequence the bootstrap ran against kind.
    install_flux_operator(&cfg.repo, registry_config, Some(kc)).await?;
    if let Some(github) = preflight.github.as_ref() {
        create_github_secrets(&cfg.repo, github, Some(kc)).await?;
    }
    install_flux_instance(cfg, preflight.github.as_ref(), registry_config, Some(kc)).await?;

    println!(">>> Kustomizations on the management cluster:");
    run(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &[
                "get",
                "kustomizations",
                "-n",
                &cfg.repo.bootstrap.flux_namespace,
            ],
        ),
    )
    .await?;
    Ok(())
}

/// Phase 6: guarded kind deletion.
async fn pivot_delete_bootstrap_cluster(
    cfg: &Config,
    http: &reqwest::Client,
    kc: &str,
    engine: &str,
) -> Result<()> {
    if cfg.pivot_skip_delete {
        println!(">>> PIVOT_SKIP_DELETE=1: keeping the kind bootstrap cluster for inspection");
        return Ok(());
    }

    // Guard: only delete kind once the management cluster demonstrably owns
    // everything (all clusters present; local-host: registry still serving).
    let clusters = capture_lossy(
        "kubectl",
        &kubectl_cmd(
            Some(kc),
            &[
                "get",
                "clusters",
                "-n",
                &cfg.repo.bootstrap.mgmt_namespace,
                "-o",
                "name",
            ],
        ),
    )
    .await;
    if clusters.lines().map(str::trim).all(|l| l.is_empty()) {
        eprintln!("ERROR: no Clusters on the management cluster; refusing to delete kind.");
        bail!("no Clusters on the management cluster; refusing to delete kind");
    }
    if cfg.is_local() && !registry_reachable(cfg, http).await {
        eprintln!(
            "ERROR: local registry unavailable; the management cluster's Flux depends on it."
        );
        eprintln!("       Refusing to delete kind until it is serving.");
        bail!("local registry unavailable; refusing to delete kind");
    }

    println!(">>> Deleting the kind bootstrap cluster...");
    // The toolbox must leave the kind network first: kind removes the
    // network with the last node, and an attached toolbox would keep it alive.
    engine::toolbox_leave_kind_network(cfg, engine).await;
    run(
        "kind",
        &[
            "delete",
            "cluster",
            "--name",
            &cfg.repo.bootstrap.kind_cluster,
        ],
    )
    .await?;

    println!();
    println!(">>> Pivot complete: the management cluster is self-managed.");
    println!(
        ">>> Management kubeconfig: {}",
        cfg.mgmt_kubeconfig.display()
    );
    println!(
        ">>> Use with: KUBECONFIG={} kubectl get clusters",
        cfg.mgmt_kubeconfig.display()
    );
    let mgmt_context = &cfg.repo.bootstrap.mgmt_context;
    if run_quiet("kubectl", &["config", "use-context", mgmt_context]).await {
        println!(">>> kubectl context switched to {mgmt_context}");
    } else {
        println!(
            ">>> To use it by default: export KUBECONFIG={}",
            cfg.mgmt_kubeconfig.display()
        );
    }
    Ok(())
}

/// The pivot: pivot.sh's phases in order, with pivot.sh's messages.
async fn run_pivot(cfg: &Config, preflight: &Preflight, http: &reqwest::Client) -> Result<()> {
    let mgmt_cluster = &cfg.environment.mgmt_cluster;

    // ── Phase 0: preflight ────────────────────────────────────────────────
    // Required tools were already checked by the bootstrap preflight
    // (clusterctl and mise are required on both profiles for the pivot);
    // the aws flux-env checks ran in preflight_github; the GitHub branch
    // check is not repeated here.
    pivot_check_context(cfg).await?;
    if cfg.is_local() && !registry_reachable(cfg, http).await {
        eprintln!(
            "ERROR: local registry is unavailable at localhost:{}",
            cfg.registry_port
        );
        eprintln!("       The management cluster's Flux syncs from it; it must stay running.");
        bail!(
            "local registry is unavailable at localhost:{}",
            cfg.registry_port
        );
    }
    // The management Cluster object is created by Flux after the bootstrap
    // handoff (issue #348): poll for it, surfacing failed Kustomizations on
    // timeout, instead of failing fast on a clean first run.
    wait_for_mgmt_cluster_definition(cfg).await?;

    // ── Phase 1: wait for the management cluster ──────────────────────────
    pivot_wait_for_management_cluster(cfg, mgmt_cluster).await?;

    // ── Phase 2: export the target kubeconfig ─────────────────────────────
    pivot_export_kubeconfig(cfg, &preflight.engine, mgmt_cluster).await?;
    let kc = cfg.mgmt_kubeconfig.to_string_lossy().into_owned();

    // Anonymous registry config shared by the target helm installs, like
    // the bootstrap's (the temp file is removed when it drops).
    let mut registry_config =
        tempfile::NamedTempFile::new().context("failed to create temp registry config")?;
    {
        use std::io::Write;
        writeln!(registry_config, "{{}}")?;
        registry_config.flush()?;
    }

    // ── Phase 3: install CAPI in the target ───────────────────────────────
    pivot_install_capi_in_target(cfg, registry_config.path(), &kc).await?;

    // ── Phase 4: suspend Flux in kind, then move ──────────────────────────
    pivot_suspend_and_move(cfg, &kc).await?;

    // ── Phase 5: seed Flux on the target ──────────────────────────────────
    pivot_seed_target(cfg, preflight, registry_config.path(), &kc).await?;

    // ── Phase 6: delete the bootstrap cluster ─────────────────────────────
    pivot_delete_bootstrap_cluster(cfg, http, &kc, &preflight.engine).await?;
    Ok(())
}

// ── Main ──────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    // Must run before any engine probe below, in either subcommand branch.
    engine::ensure_toolbox_container_host(
        std::env::var("KROPS_TOOLBOX").is_ok_and(|value| value == "1"),
    );

    // Load the repository config first: profile names, defaults, and chart
    // pins all come from it (BOOTSTRAP_CONFIG overrides the location).
    let repo = BootstrapConfig::locate_and_load()?;
    let cli = Cli::parse();

    // The teardown subcommand has its own knob set; it does not run the
    // bootstrap Config resolution (which validates bootstrap/pivot knobs).
    if let Some(SubCommand::Teardown { profile }) = &cli.command {
        let name = resolve_environment(
            std::env::var("KROPS_PROFILE")
                .ok()
                .filter(|v| !v.is_empty())
                .as_deref(),
            profile.as_deref(),
            &repo,
        )?;
        let environment = repo
            .environment(&name)
            .with_context(|| "internal: environment vanished between resolution and lookup")?
            .clone();
        // A minimal Config carrying what teardown needs (profile,
        // environment section, repository config).
        let cfg = Config {
            repo,
            profile: name,
            environment,
            recreate: false,
            registry_port: DEFAULT_REGISTRY_PORT,
            registry_ready_retries: DEFAULT_REGISTRY_READY_RETRIES,
            local_reconcile_timeout: DEFAULT_LOCAL_RECONCILE_TIMEOUT.to_string(),
            container_engine: std::env::var("CONTAINER_ENGINE").ok(),
            engine_sock: std::env::var("ENGINE_SOCK").ok(),
            toolbox: std::env::var("KROPS_TOOLBOX").is_ok_and(|value| value == "1"),
            git_repo_url: None,
            github_token: None,
            github_user: DEFAULT_GITHUB_USER.to_string(),
            age_key_file: PathBuf::from(DEFAULT_AGE_KEY_FILE),
            age_public_key: None,
            oci_repository: DEFAULT_OCI_REPOSITORY.to_string(),
            oci_tag: DEFAULT_OCI_TAG.to_string(),
            bootstrap_pivot: true,
            pivot_skip_delete: false,
            mgmt_kubeconfig: teardown::TeardownConfig::from_env(|n| std::env::var(n).ok())?
                .mgmt_kubeconfig,
            mgmt_ready_timeout: String::new(),
            mgmt_poll_interval: DEFAULT_MGMT_POLL_INTERVAL,
            bootstrap_kubecontext: String::new(),
        };
        let tcfg = teardown::TeardownConfig::from_env(|n| std::env::var(n).ok())?;
        return teardown::run_teardown(&cfg, &tcfg).await;
    }

    let cfg = Config::load(&cli, repo)?;
    let http = reqwest::Client::builder()
        .user_agent(concat!("krops-bootstrap/", env!("CARGO_PKG_VERSION")))
        .connect_timeout(HTTP_CONNECT_TIMEOUT)
        .timeout(HTTP_REQUEST_TIMEOUT)
        .build()
        .context("failed to build HTTP client")?;

    // Parity with the script: cleanup runs on the normal exit and error
    // paths (temp files drop with their owners, the flux logs follower is
    // killed by its guard). Complete signal-driven cancellation is deferred
    // to a focused follow-up rather than shipped half-implemented here.
    run_bootstrap(&cfg, &http).await
}

async fn run_bootstrap(cfg: &Config, http: &reqwest::Client) -> Result<()> {
    let preflight = preflight_checks(cfg, http).await?;
    println!(
        ">>> Using container engine: {} (socket: {})",
        preflight.engine, preflight.engine_sock
    );

    // Step 1: ensure the kind management cluster (reuse by default; --recreate replaces).
    ensure_kind_cluster(cfg, &preflight.engine, &preflight.engine_sock).await?;

    // Optional provider hook (issue #236): runs on both the fresh-create and
    // healthy-reuse paths so a rerun re-asserts the setup (e.g. azure Arc
    // federation). A non-zero exit aborts the bootstrap before Flux installs.
    if let Some(task) = &cfg.environment.post_kind_create_task {
        println!(
            ">>> Running post-kind-create task '{task}' (mise -E {} run {task})...",
            cfg.profile
        );
        run("mise", &post_kind_create_hook_args(&cfg.profile, task)).await?;
    }

    // Step 1.5: bootstrap the local container registry (local-host only).
    if cfg.is_local() {
        bootstrap_local_registry(cfg, &preflight.engine, http).await?;
    }

    // Anonymous registry config shared by both helm installs; the temp file is
    // removed automatically when it drops at the end of main.
    let mut registry_config =
        tempfile::NamedTempFile::new().context("failed to create temp registry config")?;
    {
        use std::io::Write;
        writeln!(registry_config, "{{}}")?;
        registry_config.flush()?;
    }

    // Step 2: install the Flux Operator.
    install_flux_operator(&cfg.repo, registry_config.path(), None).await?;

    // Step 3: GitHub PAT + SOPS age secrets (github-sync environments).
    if let Some(github) = preflight.github.as_ref() {
        create_github_secrets(&cfg.repo, github, None).await?;
    }

    // Step 4: install the FluxInstance via Helm.
    let controllers_ready =
        install_flux_instance(cfg, preflight.github.as_ref(), registry_config.path(), None).await?;

    // Step 5: watch local-host reconciliation.
    if cfg.is_local() {
        watch_local_reconciliation(cfg, &preflight.engine).await?;
    }

    // Done. Everything else is driven by GitOps.
    println!();
    if !controllers_ready {
        println!(
            ">>> Bootstrap finished WITH WARNINGS: Flux controllers were not all ready; \
             check 'kubectl -n flux-system get pods' before relying on the cluster"
        );
    }
    if runs_github_preflight(cfg) {
        let url = preflight
            .github
            .as_ref()
            .map(|a| a.git_repo_url.as_str())
            .unwrap_or_default();
        println!(">>> Bootstrap complete! Flux is now reconciling from {url}");
        println!(">>> Watch progress with: flux get kustomizations --watch");
    } else {
        let registry_name = &cfg.repo.bootstrap.registry_name;
        println!(">>> Bootstrap complete: Flux is reconciling from the local OCI artifact");
        println!(
            ">>> Local registry: localhost:{port} (cluster endpoint: {registry_name}:5000)",
            port = cfg.registry_port
        );
        println!(
            ">>> OCI source: oci://{registry_name}:5000/{repo}:{tag} (path: {})",
            cfg.environment.sync_path,
            repo = cfg.oci_repository,
            tag = cfg.oci_tag
        );
        println!(">>> Watch progress with: flux get sources oci --watch");
        println!(">>> No AWS resources were provisioned");
    }

    // The default exit of the bootstrap is the pivot (#95): move the CAPI
    // inventory into the self-managed management cluster, then delete the
    // kind bootstrap cluster (bootstrap.sh `exec pivot.sh` unless
    // BOOTSTRAP_PIVOT=0).
    if cfg.bootstrap_pivot {
        run_pivot(cfg, &preflight, http).await?;
    }
    Ok(())
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    /// The repository's checked-in bootstrap.toml, as tests' base config.
    fn repo_config() -> BootstrapConfig {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"));
        BootstrapConfig::load_from(&root.join("../bootstrap.toml")).unwrap()
    }

    fn config_from(cli: &Cli, get: impl Fn(&str) -> Option<String>) -> Config {
        Config::from_env(cli, repo_config(), get).unwrap()
    }

    // A Config skeleton for tests that only exercise profile-gated logic.
    fn teardown_minimal_config() -> Config {
        let cli = Cli::try_parse_from(["krops-bootstrap", "aws"]).unwrap();
        Config::from_env(&cli, repo_config(), |_| None).unwrap()
    }

    const VALID_AGE_KEY: &str = "# created: 2026-01-01T00:00:00+02:00\n# public key: age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\nAGE-SECRET-KEY-1SECRETSECRETSECRET\n";

    #[test]
    fn cli_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn cli_rejects_unknown_profile() {
        // Positional profile names are validated against bootstrap.toml at
        // resolution time (they name [environments.*] sections), not by clap.
        let cli = Cli::try_parse_from(["krops-bootstrap", "bogus"]).unwrap();
        assert!(Config::from_env(&cli, repo_config(), |_| None).is_err());
    }

    #[test]
    fn cli_rejects_deferred_flags() {
        // The expanded flag interface is deferred (review follow-up): the
        // CLI accepts only the positional profile and --recreate.
        for rejected in [
            ["krops-bootstrap", "--registry-port", "5500"],
            ["krops-bootstrap", "--github-token", "x"],
            ["krops-bootstrap", "--oci-tag", "dev"],
            ["krops-bootstrap", "--container-engine", "docker"],
            // Pivot knobs are env-only (BOOTSTRAP_PIVOT / PIVOT_SKIP_DELETE),
            // per the #95 entry-point decision: no new CLI surface.
            ["krops-bootstrap", "aws", "--no-pivot"],
            ["krops-bootstrap", "aws", "--pivot"],
            ["krops-bootstrap", "--mgmt-kubeconfig", "/tmp/x.yaml"],
            // Teardown knobs are env-only too (AWS_ONLY /
            // FORCE_KIND_DELETE), matching teardown.sh's interface.
            ["krops-bootstrap", "teardown", "--aws-only"],
            ["krops-bootstrap", "teardown", "--force-kind-delete"],
        ] {
            assert!(
                Cli::try_parse_from(rejected).is_err(),
                "{rejected:?} should not parse"
            );
        }
    }

    #[test]
    fn config_defaults_match_script() {
        let cfg = config_from(
            &Cli::try_parse_from(["krops-bootstrap", "local-host"]).unwrap(),
            |_| None,
        );
        assert_eq!(cfg.profile, "local-host");
        assert!(!cfg.recreate);
        assert_eq!(cfg.registry_port, 5001);
        assert_eq!(cfg.registry_ready_retries, 120);
        assert_eq!(cfg.local_reconcile_timeout, "15m");
        assert_eq!(cfg.github_user, "git");
        assert_eq!(cfg.age_key_file, PathBuf::from("age.agekey"));
        assert_eq!(cfg.oci_repository, "krops");
        assert_eq!(cfg.oci_tag, "latest");
        assert!(cfg.container_engine.is_none());
        assert!(cfg.git_repo_url.is_none());
        assert!(cfg.github_token.is_none());
        assert!(cfg.age_public_key.is_none());
        // Pivot defaults (pivot.sh): pivot on, keep kind off, per-profile
        // readiness timeout, 10s polls, kind-mgmt source context. The
        // per-profile timeout and context now come from bootstrap.toml.
        assert!(cfg.bootstrap_pivot);
        assert!(!cfg.pivot_skip_delete);
        assert_eq!(cfg.mgmt_ready_timeout, "15m"); // local-host default
        assert_eq!(cfg.mgmt_poll_interval, 10);
        assert_eq!(cfg.bootstrap_kubecontext, "kind-mgmt");
        assert_eq!(cfg.mgmt_kubeconfig, default_mgmt_kubeconfig());
        // Values now sourced from the repository config.
        assert_eq!(cfg.environment.mgmt_cluster, "local-management");
        assert_eq!(cfg.environment.sync_path, "mgmt/local-host");
        assert_eq!(
            (
                cfg.environment.infra_provider_namespace.as_str(),
                cfg.environment.infra_provider_name.as_str()
            ),
            ("capd-system", "docker")
        );
        assert_eq!(cfg.repo.bootstrap.registry_name, "krops-registry");
        assert_eq!(cfg.repo.bootstrap.flux_namespace, "flux-system");
        assert_eq!(cfg.repo.bootstrap.mgmt_namespace, "default");
        assert_eq!(cfg.repo.charts["flux-operator"], "0.58.0");
    }

    #[test]
    fn config_reads_env_knobs_with_script_semantics() {
        let cli = Cli::try_parse_from(["krops-bootstrap"]).unwrap();
        let get = |name: &str| -> Option<String> {
            match name {
                "REGISTRY_PORT" => Some("5500".into()),
                "LOCAL_RECONCILE_TIMEOUT" => Some("30m".into()),
                "GITHUB_USER" => Some("".into()), // empty behaves like unset
                "OCI_TAG" => Some("dev".into()),
                _ => None,
            }
        };
        let cfg = Config::from_env(&cli, repo_config(), get).unwrap();
        assert_eq!(cfg.profile, "aws"); // no env, no positional
        assert_eq!(cfg.registry_port, 5500);
        assert_eq!(cfg.local_reconcile_timeout, "30m");
        assert_eq!(cfg.github_user, "git"); // empty env fell back to default
        assert_eq!(cfg.oci_tag, "dev");
        assert_eq!(cfg.mgmt_ready_timeout, "40m"); // aws default
    }

    #[test]
    fn config_reads_pivot_knobs_with_script_semantics() {
        let cli = Cli::try_parse_from(["krops-bootstrap", "aws"]).unwrap();
        let get = |name: &str| -> Option<String> {
            match name {
                "BOOTSTRAP_PIVOT" => Some("0".into()),
                "PIVOT_SKIP_DELETE" => Some("1".into()),
                "MGMT_READY_TIMEOUT" => Some("25m".into()),
                "MGMT_POLL_INTERVAL" => Some("5".into()),
                "MGMT_KUBECONFIG" => Some("/tmp/mgmt.yaml".into()),
                "BOOTSTRAP_KUBECONTEXT" => Some("other-ctx".into()),
                _ => None,
            }
        };
        let cfg = Config::from_env(&cli, repo_config(), get).unwrap();
        assert!(!cfg.bootstrap_pivot); // only literal "1" enables
        assert!(cfg.pivot_skip_delete);
        assert_eq!(cfg.mgmt_ready_timeout, "25m");
        assert_eq!(cfg.mgmt_poll_interval, 5);
        assert_eq!(cfg.mgmt_kubeconfig, PathBuf::from("/tmp/mgmt.yaml"));
        assert_eq!(cfg.bootstrap_kubecontext, "other-ctx");
    }

    #[test]
    fn config_reads_toolbox_runtime_knobs() {
        let cli = Cli::try_parse_from(["krops-bootstrap", "local-host"]).unwrap();
        let cfg = config_from(&cli, |name| match name {
            "KROPS_TOOLBOX" => Some("1".into()),
            "ENGINE_SOCK" => Some("/run/user/501/podman/podman.sock".into()),
            _ => None,
        });

        assert!(cfg.toolbox);
        assert_eq!(
            cfg.engine_sock.as_deref(),
            Some("/run/user/501/podman/podman.sock")
        );
        assert_eq!(cfg.registry_endpoint(), ("krops-registry".into(), 5000));
        assert!(!cfg.should_rewrite_capd_endpoint());
    }

    #[test]
    fn host_runtime_keeps_localhost_endpoints() {
        let cfg = config_from(
            &Cli::try_parse_from(["krops-bootstrap", "local-host"]).unwrap(),
            |_| None,
        );

        assert!(!cfg.toolbox);
        assert!(cfg.engine_sock.is_none());
        assert_eq!(cfg.registry_endpoint(), ("localhost".into(), 5001));
        assert!(cfg.should_rewrite_capd_endpoint());
    }

    #[test]
    fn sync_source_is_config_driven() {
        // The FluxInstance sync source comes from bootstrap.toml [environments.*]
        // (issue #105 scope item 6), not from the profile name: aws and
        // local-talos sync from GitHub, local-host from the local OCI registry.
        let repo = repo_config();
        assert_eq!(
            repo.environment("aws").unwrap().sync,
            SyncSource::Github,
            "aws must declare sync = \"github\""
        );
        assert_eq!(
            repo.environment("local-talos").unwrap().sync,
            SyncSource::Github,
            "local-talos must declare sync = \"github\""
        );
        assert_eq!(
            repo.environment("local-host").unwrap().sync,
            SyncSource::Oci,
            "local-host must declare sync = \"oci\""
        );
    }

    #[test]
    fn github_preflight_runs_for_github_sync_environments() {
        // The GitHub/age preflight (PAT, repo branch probe, sops key) is
        // gated on the sync source, not the aws profile name: local-talos
        // needs the identical checks. The AWS-only credential steps stay
        // gated on the profile.
        assert!(runs_github_preflight(&Config {
            profile: "aws".into(),
            environment: repo_config().environment("aws").unwrap().clone(),
            ..teardown_minimal_config()
        }));
        assert!(runs_github_preflight(&Config {
            profile: "local-talos".into(),
            environment: repo_config().environment("local-talos").unwrap().clone(),
            ..teardown_minimal_config()
        }));
        assert!(!runs_github_preflight(&Config {
            profile: "local-host".into(),
            environment: repo_config().environment("local-host").unwrap().clone(),
            ..teardown_minimal_config()
        }));
    }

    #[test]
    fn required_tools_match_the_sync_surface() {
        // Base tools for every environment; local-host adds the local
        // reconciliation-watch and oci-push tools. local-talos needs
        // nothing beyond the base set: the machine is remote (no
        // localhost rewrite, no oci-push) and talosctl is an operator
        // convenience, not a bootstrap dependency.
        let base = ["kind", "helm", "kubectl", "clusterctl", "mise"];
        // AWS includes the aws CLI for EIP quota checks.
        let aws = required_tools(repo_config().environment("aws").unwrap());
        assert_eq!(
            aws,
            vec!["kind", "helm", "kubectl", "clusterctl", "mise", "aws"]
        );
        assert_eq!(
            required_tools(repo_config().environment("local-talos").unwrap()),
            base
        );
        let local = required_tools(repo_config().environment("local-host").unwrap());
        assert!(local.len() > base.len());
        assert!(local.contains(&"flux"));
        assert!(local.contains(&"curl"));
    }

    #[test]
    fn post_kind_create_hook_command_uses_profile_and_task() {
        let args = post_kind_create_hook_args("azure", "arc-federate");
        assert_eq!(args, vec!["-E", "azure", "run", "arc-federate"]);
    }

    #[test]
    fn substitute_manifest_vars_replaces_known_and_leaves_unknown() {
        let mut vars = std::collections::HashMap::new();
        vars.insert(
            "AZURE_SUBSCRIPTION_ID".to_string(),
            "11111111-1111-1111-1111-111111111111".to_string(),
        );
        vars.insert(
            "AZURE_TENANT_ID".to_string(),
            "22222222-2222-2222-2222-222222222222".to_string(),
        );
        let manifest = "AZURE_SUBSCRIPTION_ID: \"${AZURE_SUBSCRIPTION_ID}\"\nAZURE_TENANT_ID: \"${AZURE_TENANT_ID}\"\nAZURE_CLIENT_ID: \"${AZURE_CLIENT_ID}\"\n";
        let out = substitute_manifest_vars(manifest, &vars);
        assert!(out.contains("11111111-1111-1111-1111-111111111111"));
        assert!(out.contains("22222222-2222-2222-2222-222222222222"));
        // Unknown placeholders stay literal so the error below names them.
        assert!(out.contains("${AZURE_CLIENT_ID}"));
    }

    #[test]
    fn substitute_manifest_vars_supports_defaults() {
        // ${VAR:=default} renders the default when VAR is unset, and the
        // configured value when it is set (issue #72: GCP_WIF_PROVIDER is
        // `kind` on the bootstrap cluster, `mgmt` after the pivot override).
        let empty: std::collections::HashMap<String, String> = std::collections::HashMap::new();
        let manifest = "audience: \"//pools/krops/providers/${GCP_WIF_PROVIDER:=mgmt}\"";
        assert_eq!(
            substitute_manifest_vars(manifest, &empty),
            "audience: \"//pools/krops/providers/mgmt\""
        );
        let mut set = std::collections::HashMap::new();
        set.insert("GCP_WIF_PROVIDER".to_string(), "kind".to_string());
        assert_eq!(
            substitute_manifest_vars(manifest, &set),
            "audience: \"//pools/krops/providers/kind\""
        );
    }

    #[test]
    fn remaining_manifest_vars_ignores_placeholders_with_defaults() {
        // A ${VAR:=default} never counts as "remaining" (it has a value),
        // but a plain ${VAR} still does.
        let with_default = "a: \"${GCP_WIF_PROVIDER:=mgmt}\"";
        assert!(remaining_manifest_vars(with_default).is_empty());
        let without_default = "a: \"${GCP_WIF_PROVIDER}\"";
        assert_eq!(
            remaining_manifest_vars(without_default),
            vec!["GCP_WIF_PROVIDER".to_string()]
        );
    }

    #[test]
    fn pivot_manifests_apply_after_provider_manifests() {
        // Guard the Phase 3 ordering contract: pivot-manifests are applied
        // after provider CRs (CAPZ must exist before its identity Secret is
        // meaningful) and before pivot-sops-secrets. The ordering lives in
        // pivot_install_capi_in_target; this test pins the source order.
        let src = include_str!("main.rs");
        let providers = src
            .find("for manifest in &cfg.environment.provider_manifests")
            .unwrap();
        let plain = src
            .find("for manifest in &cfg.environment.pivot_manifests")
            .unwrap();
        let sops = src
            .find("for manifest in &cfg.environment.pivot_sops_secrets")
            .unwrap();
        assert!(
            providers < plain,
            "pivot-manifests must apply after provider-manifests"
        );
        assert!(
            plain < sops,
            "pivot-manifests must apply before pivot-sops-secrets"
        );
    }

    #[test]
    fn pivot_manifest_vars_apply_before_substitution() {
        // Guard the ordering contract: the environment overrides must be
        // merged into the ConfigMap-derived vars BEFORE any pivot manifest is
        // substituted, or the override would never reach the manifest (issue
        // #72). The override loop sits between the flux_namespace_vars read
        // and the substitution loop.
        let src = include_str!("main.rs");
        let read_vars = src
            .find("flux_namespace_vars(&cfg.repo.bootstrap.flux_namespace).await?")
            .unwrap();
        let apply_overrides = src
            .find("for (key, value) in &cfg.environment.pivot_manifest_vars")
            .unwrap();
        let substitute = src
            .find("let substituted = substitute_manifest_vars(&raw, &vars)")
            .unwrap();
        assert!(
            read_vars < apply_overrides,
            "pivot_manifest_vars must be read after the ConfigMap vars"
        );
        assert!(
            apply_overrides < substitute,
            "pivot_manifest_vars must be applied before substitution"
        );
    }

    #[test]
    fn toolbox_kind_kubeconfig_uses_one_explicit_file() {
        assert_eq!(
            toolbox_kubeconfig_path(true, Some("/state/kind.yaml")).unwrap(),
            Some(PathBuf::from("/state/kind.yaml"))
        );
        assert!(toolbox_kubeconfig_path(true, None)
            .unwrap_err()
            .to_string()
            .contains("KUBECONFIG"));
        assert!(toolbox_kubeconfig_path(true, Some("/a:/b"))
            .unwrap_err()
            .to_string()
            .contains("single file"));
        assert_eq!(toolbox_kubeconfig_path(false, None).unwrap(), None);
    }

    #[test]
    fn internal_kind_kubeconfig_command_targets_the_named_cluster() {
        assert_eq!(
            internal_kind_kubeconfig_args("mgmt"),
            ["get", "kubeconfig", "--internal", "--name", "mgmt"]
        );
    }

    #[test]
    fn config_rejects_invalid_poll_interval() {
        let cli = Cli::try_parse_from(["krops-bootstrap", "aws"]).unwrap();
        let err = Config::from_env(&cli, repo_config(), |name| match name {
            "MGMT_POLL_INTERVAL" => Some("often".into()),
            _ => None,
        })
        .unwrap_err();
        assert!(err.to_string().contains("MGMT_POLL_INTERVAL"));
    }

    #[test]
    fn config_rejects_zero_poll_interval() {
        // 0 parses as u64 but divides by zero in the Phase 1 attempt
        // arithmetic; reject it at startup like any other invalid value.
        let cli = Cli::try_parse_from(["krops-bootstrap", "aws"]).unwrap();
        let err = Config::from_env(&cli, repo_config(), |name| match name {
            "MGMT_POLL_INTERVAL" => Some("0".into()),
            _ => None,
        })
        .unwrap_err();
        assert!(err.to_string().contains("MGMT_POLL_INTERVAL"));
    }

    #[test]
    fn config_rejects_invalid_ready_timeout() {
        let cli = Cli::try_parse_from(["krops-bootstrap", "aws"]).unwrap();
        let err = Config::from_env(&cli, repo_config(), |name| match name {
            "MGMT_READY_TIMEOUT" => Some("abc".into()),
            _ => None,
        })
        .unwrap_err();
        assert!(err.to_string().contains("MGMT_READY_TIMEOUT"));
    }

    #[test]
    fn pivot_duration_parsing_matches_script_arithmetic() {
        assert_eq!(parse_duration_seconds("40m").unwrap(), 2400);
        assert_eq!(parse_duration_seconds("15m").unwrap(), 900);
        assert_eq!(parse_duration_seconds("90s").unwrap(), 90);
        assert_eq!(parse_duration_seconds("2h").unwrap(), 7200);
        assert_eq!(parse_duration_seconds("1200").unwrap(), 1200); // bare seconds
        assert_eq!(parse_duration_seconds(" 30m ").unwrap(), 1800); // trimmed
        for bad in ["", "m", "40x", "4-0m", "forty"] {
            assert!(parse_duration_seconds(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn config_file_carries_the_git_layout() {
        // The provider lists and infra providers previously compiled into
        // provider_manifests()/infra_provider() now live in bootstrap.toml;
        // the Python cross-check (mise run validate) additionally verifies
        // every declared path exists on disk.
        let cfg = config_from(
            &Cli::try_parse_from(["krops-bootstrap", "aws"]).unwrap(),
            |_| None,
        );
        assert_eq!(cfg.environment.mgmt_cluster, "eu-north-1-management");
        assert_eq!(
            cfg.environment.provider_manifests,
            vec![
                "mgmt/aws/capi-providers/capi-system/namespace.yaml",
                "mgmt/aws/capi-providers/capi-system/providers.yaml",
                "mgmt/aws/capi-providers/capa-system/namespace.yaml",
                "mgmt/aws/capi-providers/capa-system/providers.yaml",
                "mgmt/aws/capi-providers/caaph-system/namespace.yaml",
                "mgmt/aws/capi-providers/caaph-system/addon-provider.yaml",
            ]
        );
        // The AWSClusterControllerIdentity move fallback (previously a
        // hardcoded path) is declared per environment.
        assert_eq!(cfg.environment.move_fallbacks.len(), 1);
        assert_eq!(
            cfg.environment.move_fallbacks[0].manifest,
            "mgmt/aws/infrastructure/aws-identity/identity.yaml"
        );

        let cfg = config_from(
            &Cli::try_parse_from(["krops-bootstrap", "local-host"]).unwrap(),
            |_| None,
        );
        assert_eq!(cfg.environment.mgmt_cluster, "local-management");
        assert!(cfg.environment.move_fallbacks.is_empty());
    }

    #[test]
    fn config_reads_environments_in_file_order() {
        // The unsupported-profile error lists environments in file order
        // ('local-host' before 'aws'), preserving bootstrap.sh's wording.
        let repo = repo_config();
        let err = resolve_environment(Some("bogus"), None, &repo).unwrap_err();
        assert_eq!(
            err.to_string(),
            "unsupported profile 'bogus' (expected 'local-host' or 'aws' or 'local-talos' or 'azure' or 'gcp')"
        );
    }

    #[test]
    fn suspend_and_unpause_patch_payloads_match_script() {
        assert_eq!(suspend_patch().to_string(), r#"{"spec":{"suspend":true}}"#);
        assert_eq!(unpause_patch().to_string(), r#"{"spec":{"paused":false}}"#);
    }

    #[test]
    fn config_rejects_non_numeric_registry_port() {
        let cli = Cli::try_parse_from(["krops-bootstrap", "local-host"]).unwrap();
        let err = Config::from_env(&cli, repo_config(), |name| match name {
            "REGISTRY_PORT" => Some("not-a-port".into()),
            _ => None,
        })
        .unwrap_err();
        assert!(err.to_string().contains("REGISTRY_PORT"));
    }

    #[test]
    fn environment_resolution_matches_script_precedence() {
        let repo = repo_config();
        // Non-empty env wins over the positional, like ${KROPS_PROFILE:-${1:-aws}}.
        assert_eq!(
            resolve_environment(Some("aws"), Some("local-host"), &repo).unwrap(),
            "aws"
        );
        assert_eq!(
            resolve_environment(Some("local-host"), Some("aws"), &repo).unwrap(),
            "local-host"
        );
        // Empty env falls through to the positional, then the config's
        // default-environment (aws).
        assert_eq!(
            resolve_environment(Some(""), Some("local-host"), &repo).unwrap(),
            "local-host"
        );
        assert_eq!(resolve_environment(Some(""), None, &repo).unwrap(), "aws");
        assert_eq!(
            resolve_environment(None, Some("aws"), &repo).unwrap(),
            "aws"
        );
        assert_eq!(resolve_environment(None, None, &repo).unwrap(), "aws");
        // Unknown env values fail with the script's error message.
        assert_eq!(
            resolve_environment(Some("bogus"), Some("local-host"), &repo)
                .unwrap_err()
                .to_string(),
            "unsupported profile 'bogus' (expected 'local-host' or 'aws' or 'local-talos' or 'azure' or 'gcp')"
        );
    }

    #[test]
    fn cli_accepts_recreate_flag() {
        let cli = Cli::try_parse_from(["krops-bootstrap", "aws", "--recreate"]).unwrap();
        assert!(cli.recreate);
    }

    #[test]
    fn parse_github_repo_accepts_https_urls() {
        for url in [
            "https://github.com/polarsquad/krops",
            "https://github.com/polarsquad/krops/",
            "https://github.com/polarsquad/krops.git",
        ] {
            assert_eq!(parse_github_repo(url), Some("polarsquad/krops"), "{url}");
        }
    }

    #[test]
    fn parse_github_repo_rejects_bad_urls() {
        for url in [
            "git@github.com:polarsquad/krops.git",
            "https://gitlab.com/polarsquad/krops",
            "https://github.com/polarsquad",
            "https://github.com//krops",
            "https://github.com/polarsquad/",
            "",
        ] {
            assert_eq!(parse_github_repo(url), None, "{url}");
        }
    }

    #[test]
    fn age_key_validation_passes_valid_file() {
        assert!(validate_age_key(VALID_AGE_KEY).is_empty());
        assert_eq!(
            extract_age_pubkey(VALID_AGE_KEY).as_deref(),
            Some("age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq")
        );
    }

    #[test]
    fn age_key_validation_reports_all_missing_fields() {
        let missing = validate_age_key("garbage\n");
        assert_eq!(
            missing,
            vec![
                "# created: header",
                "# public key: comment",
                "AGE-SECRET-KEY- line"
            ]
        );
        let missing = validate_age_key("# created: now\nAGE-SECRET-KEY-1X\n");
        assert_eq!(missing, vec!["# public key: comment"]);
    }

    #[test]
    fn resolve_age_pubkey_accepts_matching_override() {
        let file_key = extract_age_pubkey(VALID_AGE_KEY);
        let resolved = resolve_age_pubkey(file_key.as_deref(), file_key.clone()).unwrap();
        assert_eq!(resolved, file_key);
    }

    #[test]
    fn resolve_age_pubkey_rejects_mismatched_override() {
        let file_key = extract_age_pubkey(VALID_AGE_KEY).unwrap();
        let stale = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqstale0";
        let err = resolve_age_pubkey(Some(stale), Some(file_key.clone()))
            .unwrap_err()
            .to_string();
        assert!(err.contains(stale), "error must name the override: {err}");
        assert!(
            err.contains(&file_key),
            "error must name the key-file pubkey: {err}"
        );
    }

    #[test]
    fn resolve_age_pubkey_derives_from_key_file_without_override() {
        let file_key = extract_age_pubkey(VALID_AGE_KEY);
        assert_eq!(
            resolve_age_pubkey(None, file_key.clone()).unwrap(),
            file_key
        );
        // An empty-string AGE_PUBLIC_KEY counts as unset.
        assert_eq!(
            resolve_age_pubkey(Some(""), file_key.clone()).unwrap(),
            file_key
        );
    }

    #[test]
    fn resolve_age_pubkey_keeps_override_when_key_file_yields_no_pubkey() {
        // Documented setups rely on the override when the key file's public
        // key cannot be extracted; only a contradictory one is rejected.
        assert_eq!(
            resolve_age_pubkey(Some("age1override"), None)
                .unwrap()
                .as_deref(),
            Some("age1override")
        );
        assert_eq!(resolve_age_pubkey(None, None).unwrap(), None);
    }

    #[test]
    fn extract_workload_port_parses_engine_output() {
        assert_eq!(
            extract_workload_port("0.0.0.0:32771\n[::]:32771\n").as_deref(),
            Some("32771")
        );
        assert_eq!(
            extract_workload_port("127.0.0.1:6443\n").as_deref(),
            Some("6443")
        );
        assert_eq!(extract_workload_port(""), None);
        assert_eq!(extract_workload_port("garbage\n"), None);
        assert_eq!(extract_workload_port("0.0.0.0:\n"), None);
    }

    #[test]
    fn kind_config_includes_registry_patch_for_local_host_only() {
        let local = render_kind_config(true, 5001, "/var/run/docker.sock", "krops-registry");
        assert!(local.contains("containerdConfigPatches"));
        assert!(local.contains("localhost:5001"));
        assert!(local.contains("http://krops-registry:5000"));
        assert!(local.contains("hostPath: /var/run/docker.sock"));

        let aws = render_kind_config(false, 5001, "/var/run/docker.sock", "krops-registry");
        assert!(!aws.contains("containerdConfigPatches"));
        assert!(aws.contains("kind: Cluster"));
        assert!(aws.contains("role: control-plane"));
    }

    #[test]
    fn required_tools_cover_every_invoked_binary() {
        // The pivot invokes clusterctl and mise on every environment.
        let repo = repo_config();
        let aws = required_tools(repo.environment("aws").unwrap());
        // AWS includes the aws CLI for EIP quota checks.
        assert_eq!(
            aws,
            vec!["kind", "helm", "kubectl", "clusterctl", "mise", "aws"]
        );
        let local = required_tools(repo.environment("local-host").unwrap());
        assert_eq!(
            local,
            vec![
                "kind",
                "helm",
                "kubectl",
                "clusterctl",
                "mise",
                "flux",
                "curl"
            ]
        );
    }

    #[test]
    fn sops_required_when_pivot_secrets_declared() {
        // Environments declaring pivot-sops-secrets need sops on PATH (the
        // pivot decrypts them with the operator's age key); the others
        // don't. Azure moved to pivot-manifests (issue #236), so it no
        // longer declares SOPS secrets; build a synthetic environment to
        // keep proving the rule against the shipped config.
        let repo = repo_config();
        let azure = required_tools(repo.environment("azure").unwrap());
        assert!(!azure.contains(&"sops"));
        let aws = required_tools(repo.environment("aws").unwrap());
        assert!(!aws.contains(&"sops"));

        let root = Path::new(env!("CARGO_MANIFEST_DIR"));
        let raw = std::fs::read_to_string(root.join("../bootstrap.toml")).unwrap();
        let with_sops = format!(
            "{raw}\n[environments.sops-demo]\nkind = \"sops-demo\"\nsync = \"github\"\n\
             sync-path = \"mgmt/aws\"\nmgmt-cluster = \"demo-mgmt\"\nmgmt-ready-timeout = \"5m\"\n\
             infra-provider-namespace = \"capa-system\"\ninfra-provider-name = \"aws\"\n\
             provider-manifests = []\n\
             pivot-sops-secrets = [\"mgmt/aws/infrastructure/aws-identity/identity.yaml\"]\n"
        );
        let config: BootstrapConfig = toml::from_str(&with_sops).unwrap();
        let demo = required_tools(config.environment("sops-demo").unwrap());
        assert!(demo.contains(&"sops"));
    }

    #[test]
    fn secret_manifests_keep_secrets_off_argv() {
        // The secret travels inside the JSON manifest (stdin), and the
        // manifest serializes without shell-visible arguments.
        let manifest = json!({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": { "name": "flux-github-pat", "namespace": "flux-system" },
            "type": "Opaque",
            "stringData": { "username": "git", "password": "ghp_secret\"with'quotes" },
        });
        let rendered = manifest.to_string();
        assert!(rendered.contains("ghp_secret\\\"with'quotes"));
        // Round-trips as valid JSON despite embedded quotes.
        let parsed: serde_json::Value = serde_json::from_str(&rendered).unwrap();
        assert_eq!(parsed["stringData"]["password"], "ghp_secret\"with'quotes");
    }

    #[test]
    fn sops_age_secret_key_name_embeds_pubkey() {
        let pubkey = extract_age_pubkey(VALID_AGE_KEY).unwrap();
        let manifest = json!({
            "stringData": { format!("keys.{pubkey}.agekey"): VALID_AGE_KEY },
        });
        assert!(manifest["stringData"]
            .as_object()
            .unwrap()
            .contains_key(&format!("keys.{pubkey}.agekey")));
    }

    // ── Clean-first-run waits (#348/#349) ────────────────────────────────────

    #[test]
    fn nodes_readiness_parses_kubectl_json() {
        assert_eq!(nodes_readiness(r#"{"items":[]}"#), NodesReadiness::Nodeless);
        let ready = r#"{"items":[{"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}"#;
        assert_eq!(nodes_readiness(ready), NodesReadiness::Ready);
        let not_ready =
            r#"{"items":[{"status":{"conditions":[{"type":"Ready","status":"False"}]}}]}"#;
        assert_eq!(nodes_readiness(not_ready), NodesReadiness::Pending);
        // One lagging node holds the whole wait.
        let mixed = r#"{"items":[{"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"status":{"conditions":[{"type":"Ready","status":"False"}]}}]}"#;
        assert_eq!(nodes_readiness(mixed), NodesReadiness::Pending);
        // A registered node without conditions yet is not Ready.
        assert_eq!(
            nodes_readiness(r#"{"items":[{"status":{}}]}"#),
            NodesReadiness::Pending
        );
        // kubectl failed (API briefly unreachable) or returned garbage:
        // keep waiting rather than erroring the poll.
        assert_eq!(nodes_readiness(""), NodesReadiness::Pending);
        assert_eq!(
            nodes_readiness("The connection to the server was refused"),
            NodesReadiness::Pending
        );
    }

    #[tokio::test(start_paused = true)]
    async fn poll_until_returns_on_immediate_success() {
        let calls = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counter = calls.clone();
        let found = poll_until(2400, 10, move || {
            counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            async { true }
        })
        .await;
        assert!(found);
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn poll_until_succeeds_when_object_appears_after_delay() {
        let answers =
            std::sync::Arc::new(std::sync::Mutex::new(std::collections::VecDeque::from([
                false, false, false, true,
            ])));
        let scripted = answers.clone();
        let found = poll_until(2400, 10, move || {
            let next = scripted.lock().unwrap().pop_front().unwrap_or(true);
            async move { next }
        })
        .await;
        assert!(found);
    }

    #[tokio::test(start_paused = true)]
    async fn poll_until_probes_once_past_the_budget_before_failing() {
        // A 30s budget at a 10s interval is 3 attempts, plus the final
        // past-budget probe (the until-loop tests once more before failing).
        let calls = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counter = calls.clone();
        let found = poll_until(30, 10, move || {
            counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            async { false }
        })
        .await;
        assert!(!found);
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 4);
    }

    #[tokio::test(start_paused = true)]
    async fn cluster_definition_wait_succeeds_when_object_appears() {
        let answers =
            std::sync::Arc::new(std::sync::Mutex::new(std::collections::VecDeque::from([
                false, false, true,
            ])));
        let scripted = answers.clone();
        let diagnosed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = diagnosed.clone();
        wait_for_cluster_definition(
            "test-mgmt",
            "40m",
            10,
            move || {
                let next = scripted.lock().unwrap().pop_front().unwrap_or(true);
                async move { next }
            },
            move || {
                let flag = flag.clone();
                async move { flag.store(true, std::sync::atomic::Ordering::SeqCst) }
            },
        )
        .await
        .unwrap();
        // No diagnostics on the success path.
        assert!(!diagnosed.load(std::sync::atomic::Ordering::SeqCst));
    }

    #[tokio::test(start_paused = true)]
    async fn cluster_definition_wait_surfaces_kustomizations_when_stuck() {
        let diagnosed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = diagnosed.clone();
        let err = wait_for_cluster_definition(
            "test-mgmt",
            "40m",
            10,
            || async { false },
            move || {
                let flag = flag.clone();
                async move { flag.store(true, std::sync::atomic::Ordering::SeqCst) }
            },
        )
        .await
        .unwrap_err();
        // Stuck Flux: the Kustomization conditions were surfaced, and the
        // failure names the missing object and the spent budget.
        assert!(diagnosed.load(std::sync::atomic::Ordering::SeqCst));
        assert_eq!(
            err.to_string(),
            "Cluster 'test-mgmt' was not created within 40m"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn nodes_wait_succeeds_when_nodes_register_after_delay() {
        use NodesReadiness::{Nodeless, Pending, Ready};
        let answers =
            std::sync::Arc::new(std::sync::Mutex::new(std::collections::VecDeque::from([
                Nodeless, Nodeless, Pending, Ready,
            ])));
        let scripted = answers.clone();
        wait_for_nodes_ready(
            "15m",
            10,
            move || {
                let next = scripted.lock().unwrap().pop_front().unwrap_or(Ready);
                async move { next }
            },
            || async {},
        )
        .await
        .unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn nodes_wait_fails_actionably_on_timeout() {
        let diagnosed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = diagnosed.clone();
        let err = wait_for_nodes_ready(
            "15m",
            10,
            || async { NodesReadiness::Nodeless },
            move || {
                let flag = flag.clone();
                async move { flag.store(true, std::sync::atomic::Ordering::SeqCst) }
            },
        )
        .await
        .unwrap_err();
        assert!(diagnosed.load(std::sync::atomic::Ordering::SeqCst));
        assert_eq!(
            err.to_string(),
            "management cluster nodes were not all Ready within 15m"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn nodes_wait_never_succeeds_while_nodeless() {
        // Regression pin for #349: a nodeless target must burn the whole
        // budget, never succeed early (kubectl wait --all errored here).
        let err = wait_for_nodes_ready(
            "15m",
            10,
            || async { NodesReadiness::Nodeless },
            || async {},
        )
        .await
        .unwrap_err();
        assert!(err.to_string().contains("not all Ready within 15m"));
    }

    #[test]
    fn derive_eip_requirements_default_aws_run() {
        use config::AwsWorkload;
        let workloads = vec![
            AwsWorkload {
                region: "eu-north-1".into(),
                cluster_name: "eu-north-1-workload".into(),
                eks_cluster_name: "default_eu-north-1-workload-control-plane".into(),
                rds_instance: "krops-eu-north-1-workload-db".into(),
            },
            AwsWorkload {
                region: "eu-west-1".into(),
                cluster_name: "eu-west-1-workload".into(),
                eks_cluster_name: "default_eu-west-1-workload-control-plane".into(),
                rds_instance: "krops-eu-west-1-workload-db".into(),
            },
        ];
        let reqs = derive_eip_requirements(&workloads, "eu-north-1-management").unwrap();
        // eu-north-1: 3 (workload) + 3 (management) = 6
        assert_eq!(reqs["eu-north-1"].required, 6);
        assert_eq!(reqs["eu-west-1"].required, 3);
        assert_eq!(
            reqs["eu-north-1"].clusters,
            vec!["eu-north-1-workload", "eu-north-1-management"]
        );
        assert_eq!(reqs["eu-west-1"].clusters, vec!["eu-west-1-workload"]);
    }

    #[test]
    fn derive_eip_requirements_mgmt_in_single_region() {
        use config::AwsWorkload;
        let workloads = vec![AwsWorkload {
            region: "eu-north-1".into(),
            cluster_name: "eu-north-1-workload".into(),
            eks_cluster_name: "default_eu-north-1-workload-control-plane".into(),
            rds_instance: "krops-eu-north-1-workload-db".into(),
        }];
        let reqs = derive_eip_requirements(&workloads, "eu-north-1-management").unwrap();
        assert_eq!(reqs["eu-north-1"].required, 6);
        assert_eq!(reqs.len(), 1);
    }

    #[test]
    fn derive_eip_requirements_mgmt_region_independent_of_workloads() {
        use config::AwsWorkload;
        let workloads = vec![AwsWorkload {
            region: "eu-west-1".into(),
            cluster_name: "eu-west-1-workload".into(),
            eks_cluster_name: "default_eu-west-1-workload-control-plane".into(),
            rds_instance: "krops-eu-west-1-workload-db".into(),
        }];
        // Management cluster in a different region from all workloads: its EIPs
        // must still appear in the requirements map.
        let reqs = derive_eip_requirements(&workloads, "us-east-1-management").unwrap();
        assert_eq!(reqs["eu-west-1"].required, 3);
        assert_eq!(reqs["us-east-1"].required, 3);
        assert_eq!(reqs["us-east-1"].clusters, vec!["us-east-1-management"]);
        assert_eq!(reqs.len(), 2);
    }

    #[test]
    fn derive_eip_requirements_invalid_mgmt_cluster_name() {
        use config::AwsWorkload;
        let workloads = vec![AwsWorkload {
            region: "eu-north-1".into(),
            cluster_name: "eu-north-1-workload".into(),
            eks_cluster_name: "default_eu-north-1-workload-control-plane".into(),
            rds_instance: "krops-eu-north-1-workload-db".into(),
        }];
        // Management cluster name does not end with "-management": should error
        let result = derive_eip_requirements(&workloads, "eu-north-1-mgmt");
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("does not end with '-management'"));
    }

    #[test]
    fn eip_plan_tags_match_the_capa_owner_key() {
        let repo = repo_config();
        let env = repo.environment("aws").unwrap();
        let reqs = derive_eip_requirements(&env.teardown.aws_workloads, &env.mgmt_cluster).unwrap();
        let keys: Vec<String> = reqs["eu-north-1"]
            .clusters
            .iter()
            .map(|c| teardown::capa_tag_key(c))
            .collect();
        assert_eq!(
            keys,
            vec![
                "sigs.k8s.io/cluster-api-provider-aws/cluster/eu-north-1-workload",
                "sigs.k8s.io/cluster-api-provider-aws/cluster/eu-north-1-management",
            ]
        );
        assert_eq!(reqs["eu-north-1"].required, 6);
        assert_eq!(reqs["eu-west-1"].required, 3);
    }

    #[test]
    fn aws_quota_preflight_runs_without_workloads() {
        let repo = repo_config();
        // Management-only AWS config: no workload clusters.
        let mut env = repo.environment("aws").unwrap().clone();
        env.teardown.aws_workloads = vec![];

        // The gate must fire even with no workloads.
        assert!(runs_aws_quota_preflight(&env));
        // The aws CLI must be listed as required.
        assert!(required_tools(&env).contains(&"aws"));
        // derive_eip_requirements must still return the management cluster's 3 EIPs.
        let reqs = derive_eip_requirements(&env.teardown.aws_workloads, &env.mgmt_cluster).unwrap();
        assert_eq!(reqs.len(), 1);
        assert_eq!(reqs["eu-north-1"].required, 3);
        assert_eq!(reqs["eu-north-1"].clusters, vec!["eu-north-1-management"]);

        // Non-AWS environments must not trigger the AWS quota preflight.
        for kind in &["local-host", "local-talos", "azure", "gcp"] {
            let other = repo.environment(kind).unwrap();
            assert!(
                !runs_aws_quota_preflight(other),
                "kind={kind} should not run aws quota preflight"
            );
        }
    }

    #[test]
    fn eip_shortfall_fresh_account_default_limit() {
        assert_eq!(eip_shortfall(5, 0, 0, 6), Some(1));
        assert_eq!(eip_shortfall(5, 0, 0, 3), None);
    }

    #[test]
    fn eip_shortfall_rerun_after_complete_run_passes() {
        assert_eq!(eip_shortfall(8, 6, 6, 6), None);
    }

    #[test]
    fn eip_shortfall_rerun_after_partial_run_passes() {
        assert_eq!(eip_shortfall(8, 3, 3, 6), None);
    }

    #[test]
    fn eip_shortfall_counts_foreign_eips() {
        assert_eq!(eip_shortfall(8, 5, 0, 6), Some(3));
        assert_eq!(eip_shortfall(8, 7, 3, 6), Some(2));
        assert_eq!(eip_shortfall(8, 3, 0, 6), Some(1));
        assert_eq!(eip_available(8, 7, 3), 4);
    }

    #[test]
    fn eip_shortfall_saturates() {
        assert_eq!(eip_shortfall(5, 7, 0, 3), Some(3));
        assert_eq!(eip_shortfall(5, 0, 2, 3), None);
        // owned > allocated is impossible at a single instant (owned EIPs are a
        // subset of allocated ones), but it can occur across separate describe-addresses
        // calls if an EIP is released between the total-count query and the per-cluster
        // query. saturating_sub clamps foreign to zero, so availability is the full limit.
        assert_eq!(eip_available(5, 0, 2), 5);
    }

    #[test]
    fn eip_shortfall_over_quota_desired_value() {
        // Over-quota scenario: limit=5, allocated=7, owned=0, required=3
        // Shortfall detection: available = 5 - (7 - 0) = 0, needed 3, so shortfall = 3
        assert_eq!(eip_shortfall(5, 7, 0, 3), Some(3));

        // Desired value computation: foreign + required
        // foreign = allocated.saturating_sub(owned) = 7 - 0 = 7
        // desired_value = foreign + required = 7 + 3 = 10
        let foreign = 7u32.saturating_sub(0u32);
        assert_eq!(foreign, 7);
        assert_eq!(foreign + 3, 10);
    }
}
