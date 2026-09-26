//! Orphan discovery: read-only scanning for unowned AWS resources.
//!
//! Discovers resources created by CAPA (EKS clusters, RDS instances, VPCs, NAT
//! gateways, Elastic IPs, S3 buckets) and reports those older than
//! `ORPHAN_MIN_AGE_HOURS` (default 6h). Never deletes anything; the Teardown
//! subcommand remains the only deletion path. Supported profiles: aws only
//! (local-host, local-talos, azure, gcp are refused).

use anyhow::{bail, Context, Result};
use std::process::Command;

use crate::teardown::AwsSweepTarget;

// ── Configuration ─────────────────────────────────────────────────────────────

/// Configuration for the orphan discovery run.
#[derive(Debug)]
pub struct OrphansConfig {
    /// Minimum age in hours; resources younger than this are "recent" not "orphaned".
    pub min_age_hours: u64,
    /// Optional path to write JSON report.
    pub report_json: Option<String>,
}

impl OrphansConfig {
    /// Resolve from the process environment with `${VAR:-default}` semantics.
    pub fn from_env() -> Self {
        let value = |name: &str| std::env::var(name).ok().filter(|v| !v.is_empty());
        let min_age_hours = value("ORPHAN_MIN_AGE_HOURS")
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(6);
        let report_json = value("ORPHAN_REPORT_JSON");
        Self {
            min_age_hours,
            report_json,
        }
    }
}

// ── AWS probe result ──────────────────────────────────────────────────────────

/// The outcome of an AWS API query.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProbeResult {
    /// Query succeeded; contains the full stdout.
    Present(String),
    /// Query succeeded but found nothing (confirmed absence).
    Absent,
    /// Query failed (auth, access, network, or API error); no confirmation.
    Error(String),
}

/// Run an AWS CLI command and classify the result.
///
/// Args:
/// - `aws`: path to the aws CLI binary
/// - `args`: command arguments (e.g., ["eks", "describe-cluster", ...])
/// - `not_found_markers`: strings that indicate confirmed absence (e.g., ["ResourceNotFoundException"])
///
/// Returns:
/// - `Present(stdout)` if exit 0
/// - `Absent` if stderr contains any not_found_marker (case-insensitive)
/// - `Error(stderr)` otherwise
pub fn aws_probe(aws: &str, args: &[&str], not_found_markers: &[&str]) -> ProbeResult {
    let mut cmd = Command::new(aws);
    cmd.args(args).env("AWS_PAGER", "");
    let output = match cmd.output() {
        Ok(o) => o,
        Err(e) => return ProbeResult::Error(e.to_string()),
    };
    if output.status.success() {
        return ProbeResult::Present(String::from_utf8_lossy(&output.stdout).into_owned());
    }
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    let stderr_lower = stderr.to_lowercase();
    for marker in not_found_markers {
        if stderr_lower.contains(&marker.to_lowercase()) {
            return ProbeResult::Absent;
        }
    }
    ProbeResult::Error(stderr)
}

// ── ISO-8601 to epoch parser ──────────────────────────────────────────────────

/// Parse ISO-8601 timestamp to epoch seconds.
///
/// Handles formats like:
/// - 2024-09-26T10:30:45Z
/// - 2024-09-26T10:30:45.123456Z
/// - 2024-09-26T10:30:45+02:00
/// - 2024-09-26T10:30:45-05:00
pub fn parse_iso8601(s: &str) -> Option<i64> {
    let s = s.trim();
    // Parse: YYYY-MM-DDTHH:MM:SS[.frac](Z|±HH:MM)
    if s.len() < 19 {
        return None;
    }

    // Extract the date/time part and timezone part
    let (datetime, tz_str) = if let Some(z_pos) = s.find('Z') {
        (&s[..z_pos], "Z")
    } else if let Some(plus_pos) = s.rfind('+') {
        (&s[..plus_pos], &s[plus_pos..])
    } else if let Some(minus_pos) = s.rfind('-') {
        // Must be after the date part (YYYY-MM-DD), not the date separator.
        if minus_pos > 10 {
            (&s[..minus_pos], &s[minus_pos..])
        } else {
            return None;
        }
    } else {
        return None;
    };

    // Parse YYYY-MM-DD
    if datetime.len() < 10 {
        return None;
    }
    let year: i32 = datetime[0..4].parse().ok()?;
    if datetime.as_bytes()[4] != b'-' || datetime.as_bytes()[7] != b'-' {
        return None;
    }
    let month: u32 = datetime[5..7].parse().ok()?;
    let day: u32 = datetime[8..10].parse().ok()?;

    if month < 1 || month > 12 || day < 1 || day > 31 {
        return None;
    }

    // Parse T
    if datetime.len() < 11 || datetime.as_bytes()[10] != b'T' {
        return None;
    }

    // Parse HH:MM:SS[.frac]
    let time_part = &datetime[11..];
    if time_part.len() < 8 {
        return None;
    }
    let hour: u32 = time_part[0..2].parse().ok()?;
    if time_part.as_bytes()[2] != b':' || time_part.as_bytes()[5] != b':' {
        return None;
    }
    let minute: u32 = time_part[3..5].parse().ok()?;
    let second: u32 = time_part[6..8].parse().ok()?;

    if hour >= 24 || minute >= 60 || second >= 60 {
        return None;
    }

    // Calculate days since epoch (simplified; doesn't account for leap seconds).
    // Unix epoch is 1970-01-01 00:00:00 UTC.
    let days_in_month = |y: i32, m: u32| -> u32 {
        match m {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            2 => {
                if (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0) {
                    29
                } else {
                    28
                }
            }
            _ => 0,
        }
    };

    let mut total_days: i64 = 0;
    // Days from full years before this one.
    for y in 1970..year {
        total_days += if (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0) {
            366
        } else {
            365
        };
    }
    // Days from full months in this year.
    for m in 1..month {
        total_days += days_in_month(year, m) as i64;
    }
    // Days in this month (subtract 1 because day 1 = day 0).
    total_days += (day - 1) as i64;

    let mut epoch_secs = total_days * 86400 + (hour as i64) * 3600 + (minute as i64) * 60 + (second as i64);

    // Apply timezone offset.
    if tz_str == "Z" {
        // UTC, no adjustment.
    } else {
        let (sign, offset_str) = if tz_str.starts_with('-') {
            (-1, &tz_str[1..])
        } else if tz_str.starts_with('+') {
            (1, &tz_str[1..])
        } else {
            return None;
        };
        if offset_str.len() < 5 || offset_str.as_bytes()[2] != b':' {
            return None;
        }
        let tz_hour: i64 = offset_str[0..2].parse().ok()?;
        let tz_min: i64 = offset_str[3..5].parse().ok()?;
        let tz_offset_secs = sign * (tz_hour * 3600 + tz_min * 60);
        epoch_secs -= tz_offset_secs;
    }

    Some(epoch_secs)
}

// ── Findings and reports ──────────────────────────────────────────────────────

/// A single resource discovered as an orphan candidate.
#[derive(Debug, Clone)]
pub struct OrphanFinding {
    /// Resource kind (e.g., "EKS Cluster", "RDS Instance").
    pub kind: String,
    /// AWS region.
    pub region: String,
    /// Resource identifier (cluster name, instance ID, etc.).
    pub id: String,
    /// Cluster name this resource belongs to.
    pub cluster: String,
    /// Unix epoch seconds when the resource was created.
    pub created_at: Option<i64>,
    /// True if age > min_age_hours or created_at is None.
    pub is_orphan: bool,
}

/// The full discovery report.
#[derive(Debug, Clone)]
pub struct DiscoveryReport {
    /// AWS account ID.
    pub account_id: String,
    /// All discovered findings (both orphans and recent).
    pub findings: Vec<OrphanFinding>,
    /// Query errors that prevented complete discovery.
    pub errors: Vec<String>,
    /// Unix epoch seconds when the report was generated.
    pub scanned_at: i64,
}

/// Discover orphaned resources for all sweep targets.
pub fn discover_with(
    aws: &str,
    targets: &[AwsSweepTarget],
    min_age_hours: u64,
) -> DiscoveryReport {
    let mut report = DiscoveryReport {
        account_id: String::new(),
        findings: Vec::new(),
        errors: Vec::new(),
        scanned_at: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0),
    };

    // Get account ID first.
    match aws_probe(
        aws,
        &["sts", "get-caller-identity", "--output", "text", "--query", "Account"],
        &[],
    ) {
        ProbeResult::Present(output) => {
            report.account_id = output.trim().to_string();
        }
        ProbeResult::Error(err) => {
            report.errors.push(format!("Failed to get account ID: {err}"));
        }
        ProbeResult::Absent => {
            report.errors.push("Account ID lookup returned absent (impossible)".to_string());
        }
    }

    // Capture scanned_at and account_id before the loop to avoid borrow issues.
    let scanned_at = report.scanned_at;
    let account_id = report.account_id.clone();

    // Scan each target.
    for target in targets {
        discover_eks_cluster(&mut report, aws, target, scanned_at, min_age_hours);
        discover_eks_nodegroups(&mut report, aws, target, scanned_at, min_age_hours);
        discover_rds_instance(&mut report, aws, target, scanned_at, min_age_hours);
        discover_vpc_resources(&mut report, aws, target, scanned_at, min_age_hours);
        discover_s3_buckets(&mut report, aws, target, &account_id, scanned_at, min_age_hours);
    }

    report
}

fn discover_eks_cluster(
    report: &mut DiscoveryReport,
    aws: &str,
    target: &AwsSweepTarget,
    scanned_at: i64,
    min_age_hours: u64,
) {
    match aws_probe(
        aws,
        &[
            "eks",
            "describe-cluster",
            "--name",
            &target.eks_cluster_name,
            "--region",
            &target.region,
            "--output",
            "json",
        ],
        &["ResourceNotFoundException"],
    ) {
        ProbeResult::Present(output) => {
            if let Ok(json) = serde_json::from_str::<serde_json::Value>(&output) {
                if let Some(created_at_str) = json
                    .get("cluster")
                    .and_then(|c| c.get("createdAt"))
                    .and_then(|v| v.as_str())
                {
                    let created_at = parse_iso8601(created_at_str);
                    let min_age_secs = (min_age_hours as i64) * 3600;
                    let is_orphan = match created_at {
                        Some(ts) => scanned_at - ts > min_age_secs,
                        None => true,
                    };
                    report.findings.push(OrphanFinding {
                        kind: "EKS Cluster".to_string(),
                        region: target.region.clone(),
                        id: target.eks_cluster_name.clone(),
                        cluster: target.cluster_name.clone(),
                        created_at,
                        is_orphan,
                    });
                } else {
                    // Cluster found but createdAt field missing: treat as orphan
                    report.findings.push(OrphanFinding {
                        kind: "EKS Cluster".to_string(),
                        region: target.region.clone(),
                        id: target.eks_cluster_name.clone(),
                        cluster: target.cluster_name.clone(),
                        created_at: None,
                        is_orphan: true,
                    });
                }
            }
        }
        ProbeResult::Absent => {}
        ProbeResult::Error(err) => {
            report
                .errors
                .push(format!("EKS cluster {}: {err}", target.eks_cluster_name));
        }
    }
}

fn discover_eks_nodegroups(
    report: &mut DiscoveryReport,
    aws: &str,
    target: &AwsSweepTarget,
    scanned_at: i64,
    min_age_hours: u64,
) {
    // List nodegroups for this cluster.
    match aws_probe(
        aws,
        &[
            "eks",
            "list-nodegroups",
            "--cluster-name",
            &target.eks_cluster_name,
            "--region",
            &target.region,
            "--query",
            "nodegroups[]",
            "--output",
            "text",
        ],
        &["ResourceNotFoundException"],
    ) {
        ProbeResult::Present(output) => {
            for ng_name in output.split_whitespace() {
                if ng_name.is_empty() {
                    continue;
                }
                // Describe the nodegroup to get createdAt.
                match aws_probe(
                    aws,
                    &[
                        "eks",
                        "describe-nodegroup",
                        "--cluster-name",
                        &target.eks_cluster_name,
                        "--nodegroup-name",
                        ng_name,
                        "--region",
                        &target.region,
                        "--output",
                        "json",
                    ],
                    &["ResourceNotFoundException"],
                ) {
                    ProbeResult::Present(ng_output) => {
                        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&ng_output) {
                            if let Some(created_at_str) = json
                                .get("nodegroup")
                                .and_then(|n| n.get("createdAt"))
                                .and_then(|v| v.as_str())
                            {
                                let created_at = parse_iso8601(created_at_str);
                                let min_age_secs = (min_age_hours as i64) * 3600;
                                let is_orphan = match created_at {
                                    Some(ts) => scanned_at - ts > min_age_secs,
                                    None => true,
                                };
                                report.findings.push(OrphanFinding {
                                    kind: "EKS Node Group".to_string(),
                                    region: target.region.clone(),
                                    id: ng_name.to_string(),
                                    cluster: target.cluster_name.clone(),
                                    created_at,
                                    is_orphan,
                                });
                            } else {
                                // Nodegroup found but createdAt field missing: treat as orphan
                                report.findings.push(OrphanFinding {
                                    kind: "EKS Node Group".to_string(),
                                    region: target.region.clone(),
                                    id: ng_name.to_string(),
                                    cluster: target.cluster_name.clone(),
                                    created_at: None,
                                    is_orphan: true,
                                });
                            }
                        }
                    }
                    ProbeResult::Absent => {}
                    ProbeResult::Error(err) => {
                        report.errors.push(format!(
                            "EKS nodegroup {}/{}: {err}",
                            target.eks_cluster_name, ng_name
                        ));
                    }
                }
            }
        }
        ProbeResult::Absent => {}
        ProbeResult::Error(err) => {
            report
                .errors
                .push(format!("EKS nodegroups {}: {err}", target.eks_cluster_name));
        }
    }
}

fn discover_rds_instance(
    report: &mut DiscoveryReport,
    aws: &str,
    target: &AwsSweepTarget,
    scanned_at: i64,
    min_age_hours: u64,
) {
    match aws_probe(
        aws,
        &[
            "rds",
            "describe-db-instances",
            "--db-instance-identifier",
            &target.rds_instance,
            "--region",
            &target.region,
            "--output",
            "json",
        ],
        &["DBInstanceNotFound"],
    ) {
        ProbeResult::Present(output) => {
            if let Ok(json) = serde_json::from_str::<serde_json::Value>(&output) {
                if let Some(created_at_str) = json
                    .get("DBInstances")
                    .and_then(|arr| arr.as_array())
                    .and_then(|arr| arr.first())
                    .and_then(|obj| obj.get("InstanceCreateTime"))
                    .and_then(|v| v.as_str())
                {
                    let created_at = parse_iso8601(created_at_str);
                    let min_age_secs = (min_age_hours as i64) * 3600;
                    let is_orphan = match created_at {
                        Some(ts) => scanned_at - ts > min_age_secs,
                        None => true,
                    };
                    report.findings.push(OrphanFinding {
                        kind: "RDS Instance".to_string(),
                        region: target.region.clone(),
                        id: target.rds_instance.clone(),
                        cluster: target.cluster_name.clone(),
                        created_at,
                        is_orphan,
                    });
                } else {
                    // RDS instance found but InstanceCreateTime field missing: treat as orphan
                    report.findings.push(OrphanFinding {
                        kind: "RDS Instance".to_string(),
                        region: target.region.clone(),
                        id: target.rds_instance.clone(),
                        cluster: target.cluster_name.clone(),
                        created_at: None,
                        is_orphan: true,
                    });
                }
            }
        }
        ProbeResult::Absent => {}
        ProbeResult::Error(err) => {
            report
                .errors
                .push(format!("RDS instance {}: {err}", target.rds_instance));
        }
    }
}

fn discover_vpc_resources(
    report: &mut DiscoveryReport,
    aws: &str,
    target: &AwsSweepTarget,
    scanned_at: i64,
    min_age_hours: u64,
) {
    let tag_key = target.capa_tag_key();
    // Discover VPCs with the CAPA tag.
    match aws_probe(
        aws,
        &[
            "ec2",
            "describe-vpcs",
            "--filters",
            &format!("Name=tag-key,Values={tag_key}"),
            "--region",
            &target.region,
            "--query",
            "Vpcs[].VpcId",
            "--output",
            "text",
        ],
        &[],
    ) {
        ProbeResult::Present(output) => {
            for vpc_id in output.split_whitespace() {
                if vpc_id.is_empty() {
                    continue;
                }
                // Find NAT gateways to get their creation time.
                match aws_probe(
                    aws,
                    &[
                        "ec2",
                        "describe-nat-gateways",
                        "--filters",
                        &format!("Name=vpc-id,Values={vpc_id}"),
                        "Name=state,Values=pending,available",
                        "--region",
                        &target.region,
                        "--query",
                        "NatGateways[].{Id: NatGatewayId, CreateTime: CreateTime}",
                        "--output",
                        "json",
                    ],
                    &[],
                ) {
                    ProbeResult::Present(nat_output) => {
                        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&nat_output) {
                            if let Some(nats) = json.as_array() {
                                let mut oldest_created: Option<i64> = None;
                                for nat in nats {
                                    if let Some(create_time_str) =
                                        nat.get("CreateTime").and_then(|v| v.as_str())
                                    {
                                        if let Some(ts) = parse_iso8601(create_time_str) {
                                            oldest_created = Some(match oldest_created {
                                                None => ts,
                                                Some(prev) => prev.min(ts),
                                            });
                                        }
                                    }
                                }
                                if oldest_created.is_some() {
                                    let min_age_secs = (min_age_hours as i64) * 3600;
                                    let is_orphan = match oldest_created {
                                        Some(ts) => scanned_at - ts > min_age_secs,
                                        None => true,
                                    };
                                    report.findings.push(OrphanFinding {
                                        kind: "VPC".to_string(),
                                        region: target.region.clone(),
                                        id: vpc_id.to_string(),
                                        cluster: target.cluster_name.clone(),
                                        created_at: oldest_created,
                                        is_orphan,
                                    });
                                } else {
                                    // VPC has NAT gateways but none had timestamps: record as orphan
                                    report.findings.push(OrphanFinding {
                                        kind: "VPC".to_string(),
                                        region: target.region.clone(),
                                        id: vpc_id.to_string(),
                                        cluster: target.cluster_name.clone(),
                                        created_at: None,
                                        is_orphan: true,
                                    });
                                }
                            }
                        }
                    }
                    ProbeResult::Absent => {
                        // VPC exists but no NAT gateways; record the VPC anyway.
                        report.findings.push(OrphanFinding {
                            kind: "VPC".to_string(),
                            region: target.region.clone(),
                            id: vpc_id.to_string(),
                            cluster: target.cluster_name.clone(),
                            created_at: None,
                            is_orphan: true,
                        });
                    }
                    ProbeResult::Error(err) => {
                        // VPC exists but NAT discovery failed; record the VPC and the error.
                        report
                            .errors
                            .push(format!("VPC NAT discovery for {}: {err}", vpc_id));
                        report.findings.push(OrphanFinding {
                            kind: "VPC".to_string(),
                            region: target.region.clone(),
                            id: vpc_id.to_string(),
                            cluster: target.cluster_name.clone(),
                            created_at: None,
                            is_orphan: true,
                        });
                    }
                }
            }
        }
        ProbeResult::Absent => {}
        ProbeResult::Error(err) => {
            report
                .errors
                .push(format!("VPC discovery for cluster {}: {err}", target.cluster_name));
        }
    }
}

fn discover_s3_buckets(
    report: &mut DiscoveryReport,
    aws: &str,
    target: &AwsSweepTarget,
    account_id: &str,
    scanned_at: i64,
    min_age_hours: u64,
) {
    if account_id.is_empty() {
        return; // Cannot match bucket names without account ID.
    }
    // List all buckets and match pattern.
    match aws_probe(aws, &["s3api", "list-buckets", "--output", "json"], &[]) {
        ProbeResult::Present(output) => {
            if let Ok(json) = serde_json::from_str::<serde_json::Value>(&output) {
                if let Some(buckets) = json
                    .get("Buckets")
                    .and_then(|b| b.as_array())
                {
                    for bucket in buckets {
                        if let Some(name) = bucket.get("Name").and_then(|n| n.as_str()) {
                            // Match pattern: <cluster-name>-* or krops-<cluster-name>-*
                            let matches = name.starts_with(&format!("{}-", target.cluster_name))
                                || name.starts_with(&format!("krops-{}-", target.cluster_name));
                            if matches {
                                let created_at_str = bucket
                                    .get("CreationDate")
                                    .and_then(|v| v.as_str());
                                let created_at = created_at_str.and_then(parse_iso8601);
                                let min_age_secs = (min_age_hours as i64) * 3600;
                                let is_orphan = match created_at {
                                    Some(ts) => scanned_at - ts > min_age_secs,
                                    None => true,
                                };
                                report.findings.push(OrphanFinding {
                                    kind: "S3 Bucket".to_string(),
                                    region: target.region.clone(),
                                    id: name.to_string(),
                                    cluster: target.cluster_name.clone(),
                                    created_at,
                                    is_orphan,
                                });
                            }
                        }
                    }
                }
            }
        }
        ProbeResult::Absent => {}
        ProbeResult::Error(err) => {
            report.errors.push(format!("S3 bucket listing: {err}"));
        }
    }
}

// ── Reporting ─────────────────────────────────────────────────────────────────

/// Render the report as Markdown.
pub fn render_markdown(report: &DiscoveryReport, now: i64, min_age_hours: u64) -> String {
    let mut out = String::new();
    out.push_str("# AWS e2e Orphan Report\n\n");

    // Header with timestamp.
    let scanned_time = format_epoch(report.scanned_at);
    out.push_str(&format!("**Account**: {}\n", report.account_id));
    out.push_str(&format!("**Scanned**: {}\n", scanned_time));
    out.push_str(&format!("**Min Age**: {} hours\n\n", min_age_hours));

    // Count orphans.
    let orphan_count = report.findings.iter().filter(|f| f.is_orphan).count();
    let recent_count = report.findings.len() - orphan_count;

    if report.findings.is_empty() && report.errors.is_empty() {
        out.push_str("**No resources found.**\n\n");
    } else if orphan_count > 0 {
        out.push_str(&format!("**Orphans found: {}** (Recent: {})\n\n", orphan_count, recent_count));
        out.push_str("| Kind | Region | ID | Cluster | Age (h) | Status |\n");
        out.push_str("|------|--------|----|---------|---------|---------|\n");
        for finding in &report.findings {
            if finding.is_orphan {
                let age_hours = match finding.created_at {
                    Some(ts) => (now - ts) / 3600,
                    None => -1,
                };
                let status = if age_hours < 0 {
                    "Unknown".to_string()
                } else {
                    format!("{}h old", age_hours)
                };
                out.push_str(&format!(
                    "| {} | {} | {} | {} | {} | {} |\n",
                    finding.kind,
                    finding.region,
                    finding.id,
                    finding.cluster,
                    if age_hours < 0 { "-".to_string() } else { format!("{}", age_hours) },
                    status
                ));
            }
        }
        out.push_str("\n");
    } else if recent_count > 0 {
        out.push_str(&format!("**All resources are recent** (no orphans, {} resources < {} hours old)\n\n", recent_count, min_age_hours));
    }

    if !report.errors.is_empty() {
        out.push_str("## Errors\n\n");
        for error in &report.errors {
            out.push_str(&format!("- {}\n", error));
        }
        out.push_str("\n");
    }

    out.push_str("## Remediation\n\n");
    out.push_str("Run teardown to remove orphaned resources:\n");
    out.push_str("```\nAWS_ONLY=1 mise run teardown aws\n```\n\n");
    out.push_str("**Note**: This report does not scan IAM roles/users or CloudFormation stacks (usually free).\n");

    out
}

/// Render the report as JSON.
pub fn to_json(report: &DiscoveryReport, now: i64, min_age_hours: u64) -> String {
    let orphan_count = report.findings.iter().filter(|f| f.is_orphan).count();
    let recent_count = report.findings.len() - orphan_count;

    let findings: Vec<serde_json::Value> = report
        .findings
        .iter()
        .map(|f| {
            let age_hours = match f.created_at {
                Some(ts) => (now - ts) / 3600,
                None => -1,
            };
            serde_json::json!({
                "kind": f.kind,
                "region": f.region,
                "id": f.id,
                "cluster": f.cluster,
                "created_at": f.created_at,
                "age_hours": if age_hours < 0 { serde_json::json!(null) } else { serde_json::json!(age_hours) },
                "is_orphan": f.is_orphan,
            })
        })
        .collect();

    let json = serde_json::json!({
        "account_id": report.account_id,
        "scanned_at": report.scanned_at,
        "orphan_count": orphan_count,
        "recent_count": recent_count,
        "min_age_hours": min_age_hours,
        "findings": findings,
        "errors": report.errors,
    });

    serde_json::to_string_pretty(&json).unwrap_or_else(|_| "{}".to_string())
}

/// Format an epoch timestamp (seconds since Unix epoch) as YYYY-MM-DD HH:MM:SS UTC.
fn format_epoch_utc(epoch_secs: i64) -> String {
    // Use Gregorian calendar arithmetic to compute date and time from epoch.
    const SECONDS_PER_DAY: i64 = 86400;
    const SECONDS_PER_HOUR: i64 = 3600;
    const SECONDS_PER_MINUTE: i64 = 60;

    // Compute days since epoch and seconds within the current day.
    let total_days = epoch_secs / SECONDS_PER_DAY;
    let seconds_in_day = epoch_secs % SECONDS_PER_DAY;

    // Compute hours, minutes, seconds from the remainder.
    let hours = seconds_in_day / SECONDS_PER_HOUR;
    let minutes = (seconds_in_day % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE;
    let seconds = seconds_in_day % SECONDS_PER_MINUTE;

    // Compute year, month, day from total_days (epoch = 1970-01-01).
    // Use a simplified algorithm based on 400-year cycles.
    let mut days = total_days;
    let mut year = 1970i32;

    // Fast-forward by 400-year cycles (146097 days).
    const DAYS_PER_400_YEARS: i64 = 146097;
    let cycles_400 = days / DAYS_PER_400_YEARS;
    year += (cycles_400 * 400) as i32;
    days -= cycles_400 * DAYS_PER_400_YEARS;

    // Process remaining years one by one (max 399).
    let is_leap_year = |y: i32| (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
    let days_in_year = |y: i32| if is_leap_year(y) { 366 } else { 365 };

    while days >= days_in_year(year) as i64 {
        days -= days_in_year(year) as i64;
        year += 1;
    }

    // Now compute month and day within the year.
    let days_in_month = |y: i32, m: u32| -> u32 {
        match m {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            2 => if is_leap_year(y) { 29 } else { 28 },
            _ => 0,
        }
    };

    let mut month = 1u32;
    let mut day = days as u32 + 1; // day is 1-indexed.
    while day > days_in_month(year, month) {
        day -= days_in_month(year, month);
        month += 1;
    }

    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02} UTC",
        year, month, day, hours, minutes, seconds
    )
}

/// Format an epoch timestamp as a readable string.
fn format_epoch(ts: i64) -> String {
    format_epoch_utc(ts)
}

// ── Main entry point ─────────────────────────────────────────────────────────

/// Run the orphan discovery.
pub async fn run_orphans(
    cfg: &crate::Config,
    ocfg: OrphansConfig,
) -> Result<()> {
    // Refuse non-AWS profiles.
    if !cfg.environment.teardown.aws_workloads.is_empty() {
        // AWS profile is okay.
    } else {
        bail!(
            "Orphan discovery is only supported for the 'aws' profile.\n       \
             The '{}' profile has no AWS workloads to scan.",
            cfg.profile
        );
    }

    // Never let the AWS CLI open an interactive pager.
    std::env::set_var("AWS_PAGER", "");

    // Build the sweep targets.
    let targets = crate::teardown::aws_sweep_targets(&cfg.environment, &cfg.environment.teardown);

    // Discover orphans.
    let report = discover_with("aws", &targets, ocfg.min_age_hours);

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);

    // Render markdown.
    let markdown = render_markdown(&report, now, ocfg.min_age_hours);
    println!("{markdown}");

    // Write JSON if requested.
    if let Some(json_path) = &ocfg.report_json {
        let json_str = to_json(&report, now, ocfg.min_age_hours);
        std::fs::write(json_path, json_str)
            .with_context(|| format!("failed to write {json_path}"))?;
    }

    // Return error if there were discovery errors, but do not error on orphans found.
    if !report.errors.is_empty() {
        bail!(
            "Orphan discovery completed with {} error(s); see report above",
            report.errors.len()
        );
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_iso8601_parser_z_suffix() {
        let ts = parse_iso8601("2024-09-26T10:30:45Z");
        assert!(ts.is_some());
    }

    #[test]
    fn test_iso8601_parser_with_offset() {
        let ts = parse_iso8601("2024-09-26T10:30:45+02:00");
        assert!(ts.is_some());
    }

    #[test]
    fn test_iso8601_parser_fractional() {
        let ts = parse_iso8601("2024-09-26T10:30:45.123456Z");
        assert!(ts.is_some());
    }

    #[test]
    fn test_iso8601_parser_invalid() {
        let ts = parse_iso8601("not-a-timestamp");
        assert!(ts.is_none());
    }
}
