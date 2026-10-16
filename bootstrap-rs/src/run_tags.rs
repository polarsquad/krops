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
    /// Resolve run tags from environment, profile defaults, and branch SHA.
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
            .unwrap_or_else(|| branch_sha.unwrap_or("unknown").to_string());

        let expires_at = if let Some(ttl) = get_env(KROPS_RUN_TTL).filter(|v| !v.is_empty()) {
            if ttl == "none" {
                "never".to_string()
            } else {
                let ttl_secs = crate::parse_duration_seconds(&ttl)?;
                format_rfc3339_utc(now_secs + ttl_secs as i64)
            }
        } else {
            format_rfc3339_utc(now_secs + 86400)
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
    /// Returns None if the ConfigMap lacks a data field.
    pub fn from_configmap_value(cm: &serde_json::Value) -> Option<RunTags> {
        let data = cm.get("data")?;
        Some(RunTags {
            run_id: data
                .get("KROPS_RUN_ID")
                .and_then(|v| v.as_str())
                .unwrap_or("manual")
                .to_string(),
            revision: data
                .get("KROPS_REVISION")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown")
                .to_string(),
            expires_at: data
                .get("KROPS_EXPIRES_AT")
                .and_then(|v| v.as_str())
                .unwrap_or("never")
                .to_string(),
            run_kind: data
                .get("KROPS_RUN_KIND")
                .and_then(|v| v.as_str())
                .unwrap_or("manual")
                .to_string(),
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

/// Ensure the ConfigMap exists in the source (kind) cluster; create it if absent.
/// If KROPS_RUN_ID is already set, this is a rerun; fetch existing values instead
/// of generating new ones. Otherwise, generate values and apply the ConfigMap.
pub async fn ensure_in_source(cfg: &crate::Config, branch_sha: Option<&str>) -> Result<RunTags> {
    // Check if already set (rerun-safe)
    if run_id_env_set(std::env::var(KROPS_RUN_ID).ok()) {
        // Environment already set, try to read from cluster to be consistent
        let existing = crate::capture(
            "kubectl",
            &["get", "cm", RUN_CONFIGMAP_NAME, "-n", NS, "-o", "json"],
        )
        .await;

        if let Ok(json_str) = existing {
            if let Ok(cm) = serde_json::from_str::<serde_json::Value>(&json_str) {
                if let Some(data) = cm.get("data").and_then(|d| d.as_object()) {
                    let run_id = data
                        .get(KROPS_RUN_ID)
                        .and_then(|v| v.as_str())
                        .unwrap_or("manual")
                        .to_string();
                    let revision = data
                        .get(KROPS_REVISION)
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                        .to_string();
                    let expires_at = data
                        .get(KROPS_EXPIRES_AT)
                        .and_then(|v| v.as_str())
                        .unwrap_or("never")
                        .to_string();
                    let run_kind = data
                        .get(KROPS_RUN_KIND)
                        .and_then(|v| v.as_str())
                        .unwrap_or("manual")
                        .to_string();

                    return Ok(RunTags {
                        run_id,
                        revision,
                        expires_at,
                        run_kind,
                    });
                }
            }
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
    let tags = RunTags::from_configmap_value(&cm_json)
        .ok_or_else(|| anyhow::anyhow!("ConfigMap data field missing or invalid"))?;

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
        assert!(RunTags::from_configmap_value(&cm).is_none());
    }

    #[test]
    fn source_get_args_pins_context() {
        let args = source_get_args("kind-mgmt");
        assert_eq!(&args[..2], &["--context", "kind-mgmt"]);
    }
}
