//! Container engine detection, socket resolution, and kind-network lifecycle.
//! See docs/bootstrap-cli.md ("Toolbox runtime contracts") for the design
//! rationale.

use anyhow::{bail, Context, Result};
use std::process::Stdio;
use tokio::process::Command;

use crate::{capture_lossy, run, run_quiet, Config};

/// Detect a running container engine the way bootstrap.sh does: Docker
/// first (re-detecting Podman under the `docker` CLI shim, e.g. on macOS),
/// then Podman. `None` when neither is reachable.
pub(crate) async fn detect_running() -> Option<String> {
    detect_running_as("docker", "podman").await
}

/// `detect_running`, with the Docker/Podman binaries named explicitly so
/// tests can point at stubs without mutating the process-wide `PATH`.
async fn detect_running_as(docker: &str, podman: &str) -> Option<String> {
    if run_quiet(docker, &["info"]).await {
        let version = capture_lossy(docker, &["--version"]).await;
        if version.to_lowercase().contains("podman") {
            return Some("podman".to_string());
        }
        return Some("docker".to_string());
    }
    if run_quiet(podman, &["info"]).await {
        return Some("podman".to_string());
    }
    None
}

/// Points the Podman remote client at the mounted toolbox socket. A no-op
/// outside the toolbox, and never overrides an operator-set `CONTAINER_HOST`.
pub(crate) fn ensure_toolbox_container_host(toolbox: bool) {
    if toolbox && std::env::var_os("CONTAINER_HOST").is_none() {
        std::env::set_var("CONTAINER_HOST", "unix:///var/run/docker.sock");
    }
}

/// The container engine plus the daemon-side socket path kind's node
/// `extraMounts` needs.
pub(crate) struct Resolved {
    pub engine: String,
    pub engine_sock: String,
}

/// Resolve and validate the container engine, and the daemon-side socket
/// path kind's node needs. `explicit_engine`/`explicit_sock` come from the
/// `CONTAINER_ENGINE`/`ENGINE_SOCK` overrides; `toolbox` is true inside the
/// toolbox container (`KROPS_TOOLBOX=1`).
pub(crate) async fn resolve(
    explicit_engine: Option<String>,
    explicit_sock: Option<String>,
    toolbox: bool,
) -> Result<Resolved> {
    resolve_as(explicit_engine, explicit_sock, toolbox, "docker", "podman").await
}

/// `resolve`, with the Docker/Podman binaries named explicitly so tests can
/// point at stubs without mutating the process-wide `PATH`.
async fn resolve_as(
    explicit_engine: Option<String>,
    explicit_sock: Option<String>,
    toolbox: bool,
    docker: &str,
    podman: &str,
) -> Result<Resolved> {
    let engine = match explicit_engine {
        Some(e) => e,
        None => detect_running_as(docker, podman)
            .await
            .context("No running container engine found (tried docker and podman)")?,
    };

    let detected_engine_sock = match engine.as_str() {
        "docker" => {
            if !run_quiet(docker, &["info"]).await {
                bail!("Docker daemon not running");
            }
            "/var/run/docker.sock".to_string()
        }
        "podman" => {
            if !run_quiet(podman, &["info"]).await {
                bail!("Podman is not running (is 'podman machine' started?)");
            }
            std::env::set_var("KIND_EXPERIMENTAL_PROVIDER", "podman");
            // In the toolbox, `podman info` reports the mounted client socket, not the daemon path.
            let mut sock = if toolbox {
                String::new()
            } else {
                capture_lossy(podman, &["info", "--format", "{{.Host.RemoteSocket.Path}}"])
                    .await
                    .trim()
                    .trim_start_matches("unix://")
                    .to_string()
            };
            if sock.is_empty() {
                sock = "/run/podman/podman.sock".to_string();
                if !toolbox {
                    eprintln!(
                        ">>> WARNING: Could not detect the podman API socket path; assuming {sock}"
                    );
                }
            }
            sock
        }
        other => bail!("Unsupported CONTAINER_ENGINE '{other}' (expected 'docker' or 'podman')"),
    };
    let engine_sock = explicit_sock.unwrap_or(detected_engine_sock);

    Ok(Resolved {
        engine,
        engine_sock,
    })
}

/// The toolbox container's own ID (Docker/Podman write it to /etc/hostname).
fn toolbox_container_id() -> Option<String> {
    std::fs::read_to_string("/etc/hostname")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// Whether `id` is already attached to `network`.
async fn network_member(engine: &str, network: &str, id: &str) -> bool {
    capture_lossy(engine, &["network", "inspect", network])
        .await
        .contains(id)
}

/// Attach a container to `network`, idempotently: an already-attached
/// container counts as success.
async fn join_network(engine: &str, network: &str, id: &str) -> Result<()> {
    if network_member(engine, network, id).await {
        return Ok(());
    }
    let out = Command::new(engine)
        .args(["network", "connect", network, id])
        .stderr(Stdio::piped())
        .output()
        .await
        .with_context(|| format!("failed to spawn '{engine}'"))?;
    if out.status.success() || network_member(engine, network, id).await {
        return Ok(());
    }
    bail!(
        "'{engine} network connect {network} {id}' failed with {}:\n{}",
        out.status,
        String::from_utf8_lossy(&out.stderr).trim_end()
    );
}

/// Attach the toolbox container to the kind network so kind-network
/// endpoints (the internal API server, krops-registry:5000) resolve.
/// A no-op outside the toolbox.
pub(crate) async fn toolbox_join_kind_network(cfg: &Config, engine: &str) -> Result<()> {
    if !cfg.toolbox {
        return Ok(());
    }
    let Some(id) = toolbox_container_id() else {
        bail!("KROPS_TOOLBOX=1 but /etc/hostname is unreadable; cannot join the kind network");
    };
    join_network(engine, "kind", &id).await
}

/// Best-effort detach before `kind delete cluster`. kind 0.33.0's delete
/// removes the node containers but not the Docker network, so the leave is
/// detach hygiene: the toolbox must not be attached when the nodes go away.
/// A no-op outside the toolbox.
pub(crate) async fn toolbox_leave_kind_network(cfg: &Config, engine: &str) {
    if !cfg.toolbox {
        return;
    }
    if let Some(id) = toolbox_container_id() {
        let _ = run(engine, &["network", "disconnect", "kind", &id]).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Serializes every test in this module. `ensure_toolbox_container_host`
    /// mutates the process-wide `CONTAINER_HOST` env var, and `setenv`
    /// racing with another thread's `fork()` (every other test here spawns a
    /// stub subprocess) can corrupt the child's environment and fail it
    /// nondeterministically. Without this lock the suite is flaky under the
    /// default parallel test runner.
    fn test_lock() -> &'static tokio::sync::Mutex<()> {
        static LOCK: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();
        LOCK.get_or_init(|| tokio::sync::Mutex::new(()))
    }

    /// Write an executable shell stub at `dir/name` that records its argv
    /// to `log` and then runs `body` (which must end with `exit N`).
    /// Returns the stub's full path, so tests never need to touch `PATH`.
    fn stub(dir: &std::path::Path, name: &str, log: &std::path::Path, body: &str) -> String {
        let bin = dir.join(name);
        let script = format!(
            "#!/usr/bin/env sh\necho \"$@\" >> {log}\n{body}\n",
            log = log.display(),
        );
        std::fs::write(&bin, script).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        bin.to_string_lossy().into_owned()
    }

    #[test]
    fn ensure_toolbox_container_host_sets_default_only_in_toolbox() {
        let _guard = test_lock().blocking_lock();
        let original = std::env::var_os("CONTAINER_HOST");
        std::env::remove_var("CONTAINER_HOST");

        ensure_toolbox_container_host(false);
        assert!(std::env::var_os("CONTAINER_HOST").is_none());

        ensure_toolbox_container_host(true);
        assert_eq!(
            std::env::var("CONTAINER_HOST").unwrap(),
            "unix:///var/run/docker.sock"
        );

        match original {
            Some(v) => std::env::set_var("CONTAINER_HOST", v),
            None => std::env::remove_var("CONTAINER_HOST"),
        }
    }

    #[test]
    fn ensure_toolbox_container_host_never_overrides_operator_value() {
        let _guard = test_lock().blocking_lock();
        let original = std::env::var_os("CONTAINER_HOST");
        std::env::set_var("CONTAINER_HOST", "unix:///custom/podman.sock");

        ensure_toolbox_container_host(true);
        assert_eq!(
            std::env::var("CONTAINER_HOST").unwrap(),
            "unix:///custom/podman.sock"
        );

        match original {
            Some(v) => std::env::set_var("CONTAINER_HOST", v),
            None => std::env::remove_var("CONTAINER_HOST"),
        }
    }

    #[tokio::test]
    async fn detect_running_prefers_docker() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(tmp.path(), "docker", &log, "exit 0");
        let podman = stub(tmp.path(), "podman", &log, "exit 1");
        assert_eq!(
            detect_running_as(&docker, &podman).await,
            Some("docker".to_string())
        );
    }

    #[tokio::test]
    async fn detect_running_recognizes_podman_docker_shim() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        // `docker info` succeeds (the shim forwards to podman) and
        // `docker --version` reports podman.
        let docker = stub(
            tmp.path(),
            "docker",
            &log,
            "case \"$1\" in\n  info) exit 0 ;;\n  --version) echo 'podman version 5.5.0' ;;\nesac",
        );
        let podman = stub(tmp.path(), "podman", &log, "exit 1");
        assert_eq!(
            detect_running_as(&docker, &podman).await,
            Some("podman".to_string())
        );
    }

    #[tokio::test]
    async fn detect_running_falls_back_to_podman() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(tmp.path(), "docker", &log, "exit 1");
        let podman = stub(tmp.path(), "podman", &log, "exit 0");
        assert_eq!(
            detect_running_as(&docker, &podman).await,
            Some("podman".to_string())
        );
    }

    #[tokio::test]
    async fn detect_running_none_when_unreachable() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(tmp.path(), "docker", &log, "exit 1");
        let podman = stub(tmp.path(), "podman", &log, "exit 1");
        assert_eq!(detect_running_as(&docker, &podman).await, None);
    }

    #[tokio::test]
    async fn resolve_docker_uses_standard_socket() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(tmp.path(), "docker", &log, "exit 0");
        let podman = stub(tmp.path(), "podman", &log, "exit 1");
        let resolved = resolve_as(Some("docker".to_string()), None, false, &docker, &podman)
            .await
            .unwrap();
        assert_eq!(resolved.engine, "docker");
        assert_eq!(resolved.engine_sock, "/var/run/docker.sock");
    }

    #[tokio::test]
    async fn resolve_podman_inside_toolbox_never_queries_podman_info() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        // If resolve() queried `podman info --format ...` for the socket
        // path while toolbox=true, this stub would report the (wrong,
        // client-side) mounted socket instead of the static fallback.
        let docker = stub(tmp.path(), "docker", &log, "exit 1");
        let podman = stub(
            tmp.path(),
            "podman",
            &log,
            "if [ \"$1\" = info ] && [ \"$2\" = --format ]; then echo unix:///var/run/docker.sock; fi\nexit 0",
        );
        let resolved = resolve_as(Some("podman".to_string()), None, true, &docker, &podman)
            .await
            .unwrap();
        assert_eq!(resolved.engine_sock, "/run/podman/podman.sock");
    }

    #[tokio::test]
    async fn resolve_podman_outside_toolbox_queries_podman_info() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(tmp.path(), "docker", &log, "exit 1");
        let podman = stub(
            tmp.path(),
            "podman",
            &log,
            "if [ \"$1\" = info ] && [ \"$2\" = --format ]; then echo unix:///run/user/1000/podman/podman.sock; fi\nexit 0",
        );
        let resolved = resolve_as(Some("podman".to_string()), None, false, &docker, &podman)
            .await
            .unwrap();
        assert_eq!(resolved.engine_sock, "/run/user/1000/podman/podman.sock");
    }

    #[tokio::test]
    async fn resolve_engine_sock_override_wins() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(tmp.path(), "docker", &log, "exit 0");
        let podman = stub(tmp.path(), "podman", &log, "exit 1");
        let resolved = resolve_as(
            Some("docker".to_string()),
            Some("/custom/sock".to_string()),
            false,
            &docker,
            &podman,
        )
        .await
        .unwrap();
        assert_eq!(resolved.engine_sock, "/custom/sock");
    }

    #[tokio::test]
    async fn resolve_bails_on_unsupported_engine() {
        let _guard = test_lock().lock().await;
        let result = resolve_as(Some("lxc".to_string()), None, false, "docker", "podman").await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn resolve_bails_when_selected_engine_unreachable() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(tmp.path(), "docker", &log, "exit 1");
        let podman = stub(tmp.path(), "podman", &log, "exit 1");
        let result = resolve_as(Some("docker".to_string()), None, false, &docker, &podman).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn join_network_treats_existing_membership_as_success() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        // `network inspect` already reports the container ID: connect must
        // not even be attempted.
        let docker = stub(
            tmp.path(),
            "docker",
            &log,
            "case \"$1\" in\n  network) [ \"$2\" = inspect ] && echo '[{\"Containers\":{\"abc123\":{}}}]' ;;\n  *) exit 1 ;;\nesac\nexit 0",
        );
        join_network(&docker, "kind", "abc123").await.unwrap();
        let argv = std::fs::read_to_string(&log).unwrap();
        assert!(
            !argv.contains("connect"),
            "connect should not run when already a member: {argv}"
        );
    }

    #[tokio::test]
    async fn join_network_connects_when_absent() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(
            tmp.path(),
            "docker",
            &log,
            "case \"$1 $2\" in\n  \"network inspect\") echo '[{\"Containers\":{}}]' ;;\n  \"network connect\") exit 0 ;;\nesac\nexit 0",
        );
        join_network(&docker, "kind", "abc123").await.unwrap();
        let argv = std::fs::read_to_string(&log).unwrap();
        assert!(argv.contains("network connect kind abc123"));
    }

    #[tokio::test]
    async fn join_network_propagates_genuine_failure() {
        let _guard = test_lock().lock().await;
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("argv.log");
        let docker = stub(
            tmp.path(),
            "docker",
            &log,
            "case \"$1 $2\" in\n  \"network inspect\") echo '[{\"Containers\":{}}]'; exit 0 ;;\n  \"network connect\") echo 'no such network' >&2; exit 1 ;;\nesac\nexit 0",
        );
        let result = join_network(&docker, "kind", "abc123").await;
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("no such network"));
    }
}
