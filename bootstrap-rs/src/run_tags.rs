//! Run tags: environment variables and ConfigMap for tagging e2e resources (#381).
//!
//! The krops-run ConfigMap carries four tags (run-id, revision, expires-at,
//! run-kind) written imperatively during bootstrap and seeded on the pivot
//! target. Flux uses these values via postBuild substitution to tag every
//! resource kind that ACK or CAPA reconciles. Tags survive reruns (rerun-safe
//! ConfigMap lookup) and persist across the pivot (pre-seeding before Flux
//! starts on the target).

use anyhow::{Context, Result};
use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};

// Constants
const RUN_CONFIGMAP_NAME: &str = "krops-run";
const NS: &str = "flux-system";
const KROPS_RUN_ID: &str = "KROPS_RUN_ID";
const KROPS_RUN_TTL: &str = "KROPS_RUN_TTL";
const KROPS_REVISION: &str = "KROPS_REVISION";
const KROPS_OCI_GIT_SHA: &str = "KROPS_OCI_GIT_SHA";
const KROPS_EXPIRES_AT: &str = "KROPS_EXPIRES_AT";
const KROPS_RUN_KIND: &str = "KROPS_RUN_KIND";

/// Check if KROPS_RUN_ID environment variable is set and non-empty.
fn run_id_env_set(v: Option<String>) -> bool {
    v.map(|v| !v.is_empty()).unwrap_or(false)
}

/// Build kubectl args to fetch the krops-run ConfigMap from a given context.
fn source_get_args(ctx: &str) -> Vec<String> {
    vec![
        "--context".to_string(),
        ctx.to_string(),
        "get".to_string(),
        "cm".to_string(),
        RUN_CONFIGMAP_NAME.to_string(),
        "-n".to_string(),
        NS.to_string(),
        "-o".to_string(),
        "json".to_string(),
    ]
}

/// Run tags resolved from environment, defaults, or Flux ConfigMap.
pub struct RunTags {
    pub run_id: String,
    pub revision: String,
    pub expires_at: String,
    pub run_kind: String,
}

impl RunTags {
    /// Resolve run tags from environment, profile defaults, branch SHA, and
    /// the forwarded checkout SHA. The revision resolves as: KROPS_REVISION
    /// env (nonempty), then the GitHub branch SHA, then KROPS_OCI_GIT_SHA
    /// (the checkout SHA the toolbox wrapper forwards), then "unknown".
    /// If KROPS_RUN_ID env is already set, use the existing ConfigMap values
    /// (rerun-safe). Otherwise, generate new values and apply them.
    pub fn resolve(
        get_env: impl Fn(&str) -> Option<String>,
        profile: &str,
        branch_sha: Option<&str>,
        now_secs: i64,
    ) -> Result<RunTags> {
        let run_id = get_env(KROPS_RUN_ID)
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| format!("{}-{}", profile, format_rfc3339_utc(now_secs)));

        let revision = get_env(KROPS_REVISION)
            .filter(|v| !v.is_empty())
            .or_else(|| branch_sha.map(str::to_owned))
            .or_else(|| get_env(KROPS_OCI_GIT_SHA).filter(|v| !v.is_empty()))
            .unwrap_or_else(|| "unknown".to_string());

        let ttl = get_env(KROPS_RUN_TTL).unwrap_or_default();
        let ttl = ttl.trim();
        let expires_at = if !ttl.is_empty() {
            if ttl == "none" {
                "never".to_string()
            } else {
                let ttl_secs = crate::parse_duration_seconds(ttl)?;
                checked_expiry(now_secs, ttl_secs)?
            }
        } else {
            checked_expiry(now_secs, 86400)?
        };

        let run_kind = get_env(KROPS_RUN_KIND)
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "manual".to_string());

        Ok(RunTags {
            run_id,
            revision,
            expires_at,
            run_kind,
        })
    }

    /// Parse RunTags from a ConfigMap JSON value, dropping server-side metadata.
    /// Reject incomplete metadata instead of silently changing run identity.
    pub fn from_configmap_value(cm: &serde_json::Value) -> Result<RunTags> {
        let data = cm
            .get("data")
            .and_then(|v| v.as_object())
            .context("krops-run ConfigMap data missing or invalid")?;
        let field = |key: &str| -> Result<String> {
            data.get(key)
                .and_then(|v| v.as_str())
                .filter(|v| !v.is_empty())
                .map(str::to_owned)
                .with_context(|| {
                    format!("krops-run ConfigMap {key} missing, empty, or not a string")
                })
        };
        Ok(RunTags {
            run_id: field(KROPS_RUN_ID)?,
            revision: field(KROPS_REVISION)?,
            expires_at: field(KROPS_EXPIRES_AT)?,
            run_kind: field(KROPS_RUN_KIND)?,
        })
    }

    /// Render the ConfigMap as JSON for kubectl apply.
    pub fn configmap_json(&self, ns: &str) -> String {
        let cm = json!({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": RUN_CONFIGMAP_NAME,
                "namespace": ns
            },
            "data": {
                KROPS_RUN_ID: &self.run_id,
                KROPS_REVISION: &self.revision,
                KROPS_EXPIRES_AT: &self.expires_at,
                KROPS_RUN_KIND: &self.run_kind
            }
        });
        cm.to_string()
    }

    /// Display line for logging run tags.
    pub fn display_line(&self) -> String {
        format!(
            ">>> Run tags: run-id={} revision={} expires-at={} run-kind={}",
            self.run_id, self.revision, self.expires_at, self.run_kind
        )
    }
}

// Bound RFC3339 dates to four-digit years, and avoid an unbounded calendar loop.
fn checked_expiry(now: i64, ttl: u64) -> Result<String> {
    let ttl = i64::try_from(ttl).context("KROPS_RUN_TTL overflows")?;
    let expiry = now
        .checked_add(ttl)
        .context("KROPS_RUN_TTL expiry overflows")?;
    anyhow::ensure!(
        (0..=253402300799).contains(&expiry),
        "KROPS_RUN_TTL expiry overflows RFC3339"
    );
    Ok(format_rfc3339_utc(expiry))
}

/// Convert Unix epoch seconds to RFC3339 format (2026-09-27T12:00:00Z).
fn format_rfc3339_utc(secs: i64) -> String {
    // Extract time of day
    let secs_in_day = secs % 86400;
    let hour = (secs_in_day / 3600) % 24;
    let minute = (secs_in_day / 60) % 60;
    let second = secs_in_day % 60;

    // Calculate date from days since Unix epoch (1970-01-01)
    let days = secs / 86400;

    // Simple calendar algorithm: start from 1970-01-01 and count forward
    let mut year = 1970;
    let mut remaining_days = days;

    // Count years
    loop {
        let days_in_year = if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 {
            366
        } else {
            365
        };
        if remaining_days < days_in_year {
            break;
        }
        remaining_days -= days_in_year;
        year += 1;
    }

    // Days in each month
    let is_leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days_in_months = [
        31,
        if is_leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];

    let mut month = 1;
    let mut day_of_month = remaining_days + 1;
    for &days_in_month in &days_in_months {
        if day_of_month <= days_in_month {
            break;
        }
        day_of_month -= days_in_month;
        month += 1;
    }

    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year, month, day_of_month, hour, minute, second
    )
}

fn decode_existing(output: &str) -> Result<Option<RunTags>> {
    if output.trim().is_empty() {
        return Ok(None);
    }
    let cm = serde_json::from_str(output).context("invalid krops-run ConfigMap JSON")?;
    RunTags::from_configmap_value(&cm).map(Some)
}

/// Ensure the ConfigMap exists in the source (kind) cluster; create it if absent.
/// If KROPS_RUN_ID is already set, this is a rerun; fetch existing values instead
/// of generating new ones. Otherwise, generate values and apply the ConfigMap.
pub async fn ensure_in_source(cfg: &crate::Config, branch_sha: Option<&str>) -> Result<RunTags> {
    // Check if already set (rerun-safe)
    if run_id_env_set(std::env::var(KROPS_RUN_ID).ok()) {
        // Environment already set, try to read from cluster to be consistent
        let existing = crate::capture(
            "kubectl",
            &[
                "--context",
                &cfg.bootstrap_kubecontext,
                "get",
                "cm",
                RUN_CONFIGMAP_NAME,
                "-n",
                NS,
                "-o",
                "json",
                "--ignore-not-found",
            ],
        )
        .await
        .context("reading existing krops-run ConfigMap")?;
        if let Some(tags) = decode_existing(&existing)? {
            return Ok(tags);
        }

        // ConfigMap not found but KROPS_RUN_ID was set
        eprintln!(">>> Run tags: existing ConfigMap not found; generating fresh tags (run-id from KROPS_RUN_ID, expires-at recalculated)");
    }

    // Generate new tags
    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("failed to get current time")?
        .as_secs() as i64;

    let tags = RunTags::resolve(
        |name| std::env::var(name).ok(),
        &cfg.profile,
        branch_sha,
        now_secs,
    )?;

    // Apply ConfigMap
    let cm_json = serde_json::from_str::<serde_json::Value>(&tags.configmap_json(NS))?;
    crate::kubectl_apply(None, &cm_json).await?;

    println!("{}", tags.display_line());

    Ok(tags)
}

/// Seed the ConfigMap from the source (kind) cluster to the target cluster.
/// Reads using explicit --context to ensure the source cluster is consulted (not ambient context).
/// Parses cleanly to drop server metadata (resourceVersion, uid, etc.) before applying to target.
pub async fn seed_target(cfg: &crate::Config, kubeconfig: &str) -> Result<()> {
    let args_owned = source_get_args(&cfg.bootstrap_kubecontext);
    let args_str: Vec<&str> = args_owned.iter().map(|s| s.as_str()).collect();
    let json_str = crate::capture("kubectl", &args_str).await?;

    let cm_json = serde_json::from_str::<serde_json::Value>(&json_str)?;
    let tags = RunTags::from_configmap_value(&cm_json)?;

    // Rebuild the ConfigMap from data fields only (drops server metadata)
    let clean_cm = serde_json::from_str::<serde_json::Value>(&tags.configmap_json(NS))?;
    crate::kubectl_apply(Some(kubeconfig), &clean_cm).await?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resolve_explicit_run_id() {
        let tags = RunTags::resolve(
            |name| match name {
                "KROPS_RUN_ID" => Some("gha-123".to_string()),
                _ => None,
            },
            "aws",
            None,
            0,
        )
        .unwrap();
        assert_eq!(tags.run_id, "gha-123");
    }

    #[test]
    fn test_resolve_ttl_none() {
        let tags = RunTags::resolve(
            |name| match name {
                "KROPS_RUN_TTL" => Some("none".to_string()),
                _ => None,
            },
            "aws",
            None,
            0,
        )
        .unwrap();
        assert_eq!(tags.expires_at, "never");
    }

    #[test]
    fn test_rfc3339_epoch() {
        assert_eq!(format_rfc3339_utc(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn test_rfc3339_known() {
        // 2025-05-27T18:40:00Z = 1748371200 (1748304000 + 67200)
        assert_eq!(format_rfc3339_utc(1748371200), "2025-05-27T18:40:00Z");
    }

    #[test]
    fn test_rfc3339_leap_day() {
        // 2024-03-01T00:00:00Z (day after leap day 2024-02-29)
        assert_eq!(format_rfc3339_utc(1709251200), "2024-03-01T00:00:00Z");
    }

    #[test]
    fn run_id_env_set_rejects_empty() {
        assert!(!run_id_env_set(Some(String::new())));
        assert!(!run_id_env_set(None));
        assert!(run_id_env_set(Some("gha-1".to_string())));
    }

    #[test]
    fn from_configmap_value_drops_server_metadata() {
        let cm = serde_json::json!({
            "metadata": {
                "resourceVersion": "12345",
                "uid": "abc-def",
                "managedFields": []
            },
            "data": {
                "KROPS_RUN_ID": "test-run",
                "KROPS_REVISION": "abc123",
                "KROPS_EXPIRES_AT": "2026-10-01T00:00:00Z",
                "KROPS_RUN_KIND": "ci"
            }
        });
        let tags = RunTags::from_configmap_value(&cm).expect("should parse");
        assert_eq!(tags.run_id, "test-run");
        assert_eq!(tags.revision, "abc123");
        let json = tags.configmap_json(NS);
        assert!(!json.contains("resourceVersion"));
        assert!(!json.contains("uid"));
    }

    #[test]
    fn from_configmap_value_none_without_data() {
        let cm = serde_json::json!({"metadata": {}});
        assert!(RunTags::from_configmap_value(&cm).is_err());
    }

    #[test]
    fn rejects_each_incomplete_field() {
        let valid = serde_json::json!({"data": {
            "KROPS_RUN_ID": "id", "KROPS_REVISION": "sha",
            "KROPS_EXPIRES_AT": "never", "KROPS_RUN_KIND": "ci"
        }});
        for key in [
            KROPS_RUN_ID,
            KROPS_REVISION,
            KROPS_EXPIRES_AT,
            KROPS_RUN_KIND,
        ] {
            for value in [None, Some(json!("")), Some(json!(42))] {
                let mut cm = valid.clone();
                let data = cm["data"].as_object_mut().unwrap();
                match value {
                    None => {
                        data.remove(key);
                    }
                    Some(value) => {
                        data.insert(key.into(), value);
                    }
                }
                assert!(RunTags::from_configmap_value(&cm)
                    .err()
                    .unwrap()
                    .to_string()
                    .contains(key));
            }
        }
    }

    #[test]
    fn ttl_and_revision_cases() {
        for (ttl, seconds) in [
            ("3600", 3600),
            ("90s", 90),
            ("30m", 1800),
            ("2h", 7200),
            (" 0090s ", 90),
            ("0", 0),
            ("", 86400),
            ("   ", 86400),
        ] {
            let tags = RunTags::resolve(
                |key| (key == KROPS_RUN_TTL).then(|| ttl.into()),
                "aws",
                Some("branch-sha"),
                0,
            )
            .unwrap();
            assert_eq!(tags.expires_at, format_rfc3339_utc(seconds));
            assert_eq!(tags.revision, "branch-sha");
        }
        for ttl in [
            "-1",
            "+1",
            "1.5h",
            "1h30m",
            "h",
            "18446744073709551615h",
            "253402300800",
        ] {
            assert!(RunTags::resolve(
                |key| (key == KROPS_RUN_TTL).then(|| ttl.into()),
                "aws",
                None,
                0
            )
            .is_err());
        }
        assert_eq!(
            RunTags::resolve(|_| None, "aws", None, 0).unwrap().revision,
            "unknown"
        );
        assert_eq!(
            RunTags::resolve(
                |key| (key == KROPS_REVISION).then(|| "override".into()),
                "aws",
                Some("branch"),
                0
            )
            .unwrap()
            .revision,
            "override"
        );
        assert!(checked_expiry(i64::MAX, 1).is_err());
    }

    #[test]
    fn revision_precedence_env_branch_oci_unknown() {
        // KROPS_REVISION wins over branch SHA and KROPS_OCI_GIT_SHA.
        assert_eq!(
            RunTags::resolve(
                |key| match key {
                    KROPS_REVISION => Some("explicit".to_string()),
                    KROPS_OCI_GIT_SHA => Some("oci-sha".to_string()),
                    _ => None,
                },
                "aws",
                Some("branch-sha"),
                0,
            )
            .unwrap()
            .revision,
            "explicit"
        );
        // Branch SHA (GitHub sync envs) beats KROPS_OCI_GIT_SHA.
        assert_eq!(
            RunTags::resolve(
                |key| (key == KROPS_OCI_GIT_SHA).then(|| "oci-sha".to_string()),
                "aws",
                Some("branch-sha"),
                0,
            )
            .unwrap()
            .revision,
            "branch-sha"
        );
        // KROPS_OCI_GIT_SHA is the fallback where branch SHA is absent.
        assert_eq!(
            RunTags::resolve(
                |key| (key == KROPS_OCI_GIT_SHA).then(|| "oci-sha".to_string()),
                "aws",
                None,
                0,
            )
            .unwrap()
            .revision,
            "oci-sha"
        );
        // Empty KROPS_REVISION falls through to the branch SHA.
        assert_eq!(
            RunTags::resolve(
                |key| (key == KROPS_REVISION).then(String::new),
                "aws",
                Some("branch-sha"),
                0,
            )
            .unwrap()
            .revision,
            "branch-sha"
        );
        // Empty KROPS_OCI_GIT_SHA falls through to unknown.
        assert_eq!(
            RunTags::resolve(
                |key| (key == KROPS_OCI_GIT_SHA).then(String::new),
                "aws",
                None,
                0,
            )
            .unwrap()
            .revision,
            "unknown"
        );
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn command_lookup_distinguishes_absence_and_errors() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let command = dir.path().join("kubectl");
        for (body, absent, error) in [
            ("exit 0", true, false),
            ("echo '{invalid'", false, true),
            ("echo '{}'; exit 0", false, true),
            ("echo forbidden >&2; exit 1", false, true),
        ] {
            std::fs::write(&command, format!("#!/bin/sh\n{body}\n")).unwrap();
            std::fs::set_permissions(&command, std::fs::Permissions::from_mode(0o755)).unwrap();
            let result = crate::capture(command.to_str().unwrap(), &[])
                .await
                .and_then(|output| decode_existing(&output));
            assert_eq!(result.is_err(), error);
            if !error {
                assert_eq!(result.unwrap().is_none(), absent);
            }
        }
    }

    #[test]
    fn source_get_args_pins_context() {
        let args = source_get_args("kind-mgmt");
        assert_eq!(&args[..2], &["--context", "kind-mgmt"]);
    }
}
