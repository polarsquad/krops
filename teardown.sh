#!/usr/bin/env bash
# teardown.sh – Destroy all infrastructure created by bootstrap.sh, in reverse order.
#
# Destruction order:
#   1. Suspend Flux reconciliation (prevent re-creation of deleted resources)
#   2. Delete CAPI workload clusters (CAPA tears down all AWS resources per cluster)
#   3. Wait for CAPI clusters to be fully deprovisioned
#   4. Clean up orphaned AWS resources (pod identity, nodegroups, EKS, RDS,
#      VPCs, S3 buckets, IAM, CFN) — in BOTH regions ($REGIONS)
#   5. Delete CAPI providers (operator deprovisions controllers)
#   6. Uninstall the FluxInstance Helm release
#   7. Uninstall the Flux Operator Helm release
#   8. Delete the GitHub PAT, SOPS age, and AWS credentials secrets
#   9. Delete the kind management cluster
#
# AWS cleanup (step 4) targets resources that CAPA does not manage or may leave
# behind. VPC deletion is scoped exclusively to resources tagged by CAPA
# (sigs.k8s.io/cluster-api-provider-aws/cluster/{clusterName}=owned) so only
# krops infrastructure is touched.
#
# Usage:
#   ./teardown.sh              # Full teardown (k8s + AWS)
#   KROPS_PROFILE=local-host ./teardown.sh  # Delete CAPD workload + management cluster
#   AWS_ONLY=1 ./teardown.sh   # AWS orphan cleanup only (skip k8s steps)
set -euo pipefail

PROFILE="${KROPS_PROFILE:-${1:-aws}}"

preflight_checks() {
  case "$PROFILE" in
    local-host|aws) ;;
    *)
      echo "ERROR: unsupported profile '$PROFILE' (expected 'local-host' or 'aws')" >&2
      exit 1
      ;;
  esac

  if [ "$PROFILE" = local-host ]; then
    if [ "${AWS_ONLY:-0}" = "1" ]; then
      echo "ERROR: AWS_ONLY=1 cannot be combined with the local-host profile" >&2
      echo "       Use the AWS profile for AWS-only orphan cleanup" >&2
      exit 1
    fi

    for cmd in kind kubectl; do
      command -v "$cmd" >/dev/null 2>&1 \
        || { echo "ERROR: $cmd not found in PATH" >&2; exit 1; }
    done

    # Select the same running container engine used by bootstrap.sh. Registry
    # cleanup is best-effort so an unavailable engine does not block teardown.
    if [ -n "${CONTAINER_ENGINE:-}" ]; then
      case "$CONTAINER_ENGINE" in
        docker|podman) ;;
        *)
          echo "ERROR: Unsupported CONTAINER_ENGINE '${CONTAINER_ENGINE}' (expected 'docker' or 'podman')" >&2
          exit 1
          ;;
      esac
      if ! command -v "$CONTAINER_ENGINE" >/dev/null 2>&1 \
        || ! "$CONTAINER_ENGINE" info >/dev/null 2>&1; then
        echo ">>> WARNING: ${CONTAINER_ENGINE} is unavailable; registry cleanup will be skipped" >&2
        CONTAINER_ENGINE=""
      fi
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
      if docker --version 2>/dev/null | grep -qi podman; then
        CONTAINER_ENGINE=podman
      else
        CONTAINER_ENGINE=docker
      fi
    elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
      CONTAINER_ENGINE=podman
    else
      echo ">>> WARNING: No running container engine found; registry cleanup will be skipped" >&2
      CONTAINER_ENGINE=""
    fi
    return
  fi

  if [ "${AWS_ONLY:-0}" = "1" ]; then
    command -v aws >/dev/null 2>&1 \
      || { echo "ERROR: aws CLI not found in PATH (required for AWS_ONLY mode)" >&2; exit 1; }
    AWS_AVAILABLE=true
    echo ">>> AWS_ONLY mode – k8s tools not required"
    return
  fi

  for cmd in kind helm kubectl xargs; do
    command -v "$cmd" >/dev/null 2>&1 \
      || { echo "ERROR: $cmd not found in PATH" >&2; exit 1; }
  done

  AWS_AVAILABLE=false
  if command -v aws >/dev/null 2>&1; then
    AWS_AVAILABLE=true
    echo ">>> aws CLI available"
  else
    echo "!   aws CLI not found – AWS orphan cleanup (step 4) will be skipped" >&2
  fi
}

preflight_checks

if [ "$PROFILE" = local-host ]; then
  if kind get clusters 2>/dev/null | grep -q '^mgmt$'; then
    kubectl config use-context kind-mgmt >/dev/null

    # Prevent Flux from recreating the Cluster while CAPD removes its Docker
    # machines, then wait before deleting the management cluster/controller.
    kubectl patch kustomization docker-workload-cluster -n flux-system \
      --type merge -p '{"spec":{"suspend":true}}' >/dev/null 2>&1 || true
    if kubectl get cluster local-workload -n default >/dev/null 2>&1; then
      echo ">>> Deleting CAPD workload cluster 'local-workload'..."
      kubectl delete cluster local-workload -n default --wait=false
      if ! kubectl wait --for=delete cluster/local-workload -n default --timeout=5m; then
        echo "ERROR: CAPD workload cluster did not finish deleting; leaving mgmt intact" >&2
        exit 1
      fi
      echo "✓   CAPD workload cluster 'local-workload' deleted"
    fi

    echo ">>> Deleting kind management cluster 'mgmt'..."
    kind delete cluster --name mgmt
    echo "✓   kind cluster 'mgmt' deleted"
  else
    echo ">>> kind management cluster 'mgmt' is not present"
  fi

  # Clean up the local container registry used by local-host profile
  if [ -n "${CONTAINER_ENGINE:-}" ] && $CONTAINER_ENGINE ps -a --filter "name=^krops-registry$" | grep -q "krops-registry"; then
    echo ">>> Removing local registry container 'krops-registry'..."
    $CONTAINER_ENGINE rm -f krops-registry
    echo "✓   registry container 'krops-registry' removed"
  fi

  exit 0
fi

# Never let the AWS CLI open an interactive pager (it would hang the script).
export AWS_PAGER=""

# ── Configuration ──────────────────────────────────────────────────────────────
REGIONS="eu-north-1 eu-west-1"

# Global IAM roles (region-independent): the ACK controller pod-identity roles
# and the per-cluster reader roles created by the workload ACK IAM controllers
# (krops-${CLUSTER_NAME}-reader, see workload/base/iam-roles/role.yaml)
GLOBAL_IAM_ROLES="krops-ack-s3-controller krops-ack-rds-controller krops-ack-iam-controller krops-eu-north-1-workload-reader krops-eu-west-1-workload-reader"

# Global IAM users: the console reader user created by the management
# cluster's ACK IAM controller (mgmt/aws/infrastructure/aws-global-iam/
# reader-user.yaml). Users need different cleanup than roles: login profile
# (console password) + inline policies + the user itself.
GLOBAL_IAM_USERS="krops-reader"

# CloudFormation stack created by clusterawsadm bootstrap iam
CFN_STACK_NAME="cluster-api-provider-aws-sigs-k8s-io"

# ── kind teardown guard ───────────────────────────────────────────────────────
# The kind cluster runs the CAPA controller. Deleting it while CAPI clusters
# still exist ORPHANS the AWS resources (VPCs, EKS clusters, …) with no
# controller left to deprovision them. We therefore only delete kind once the
# CAPI clusters have been confirmed gone.
#
# CLUSTERS_CONFIRMED_GONE is set to 1 by step 3 once `kubectl get clusters`
# returns empty. Any early exit (error, timeout, signal) leaves it at 0, so the
# EXIT trap below refuses to delete kind and CAPA keeps running.
CLUSTERS_CONFIRMED_GONE=0

# Override: FORCE_KIND_DELETE=1 deletes kind unconditionally, accepting that
# AWS resources may be orphaned and must be removed manually.

_kind_safe_to_delete() {
  [ "${FORCE_KIND_DELETE:-0}" = "1" ] && return 0
  [ "${CLUSTERS_CONFIRMED_GONE:-0}" = "1" ] && return 0
  return 1
}

_delete_kind_if_safe() {
  if ! _kind_safe_to_delete; then
    echo ""
    warn "Refusing to delete kind cluster 'mgmt': CAPI clusters were not"
    warn "confirmed deleted. Leaving CAPA running so AWS resources can continue"
    warn "deprovisioning. Re-run teardown once 'kubectl get clusters -A' is empty,"
    warn "or set FORCE_KIND_DELETE=1 to force-delete kind and accept orphaned AWS resources."
    return 0
  fi
  info "Deleting kind management cluster 'mgmt'..."
  kind delete cluster --name mgmt 2>&1 \
    && success "kind cluster 'mgmt' deleted" \
    || warn "kind cluster 'mgmt' could not be deleted – it may already be gone"
}

# Fires on every exit (clean, error, or signal). Cluster-aware: if clusters
# were not confirmed gone, kind is left intact so CAPA can keep deprovisioning.
_teardown_kind() {
  _delete_kind_if_safe
}
trap _teardown_kind EXIT

# ── Helpers ───────────────────────────────────────────────────────────────────
info()    { echo ">>> $*"; }
success() { echo "✓   $*"; }
warn()    { echo "!   $*" >&2; }

# How long (seconds) to wait for CAPI clusters to be fully deleted before giving up.
CLUSTER_DELETE_TIMEOUT="${CLUSTER_DELETE_TIMEOUT:-1200}"
# How long (seconds) to wait for CAPI providers to be removed.
PROVIDER_DELETE_TIMEOUT="${PROVIDER_DELETE_TIMEOUT:-300}"

# ── AWS region/cluster lookup ─────────────────────────────────────────────────
# CAPA creates the EKS cluster with dashes converted to underscores.
# K8s Cluster name:  default-eu-north-1-workload-control-plane (dashes)
# EKS cluster name:  default_eu-north-1-workload-control-plane (underscore)
_get_eks_cluster() {
  case "$1" in
    eu-north-1) echo "default_eu-north-1-workload-control-plane" ;;
    eu-west-1)  echo "default_eu-west-1-workload-control-plane" ;;
    *)          return 1 ;;
  esac
}

# CLUSTER_NAME as substituted into the workload manifests (cluster-vars
# ConfigMap in mgmt/aws/addons/flux-apps/flux-instance.yaml). Used to derive
# the S3 bucket name, the CAPA ownership tag, and to sweep CAPA-created IAM
# roles by name.
_get_cluster_name() {
  case "$1" in
    eu-north-1) echo "eu-north-1-workload" ;;
    eu-west-1)  echo "eu-west-1-workload" ;;
    *)          return 1 ;;
  esac
}

# CAPA tags the VPC resources and EIPs it creates with
# sigs.k8s.io/cluster-api-provider-aws/cluster/<Cluster name>=owned
# (verified against the live account). VPC/EIP cleanup is gated on this tag so
# only krops infrastructure is ever touched.
_get_capa_tag_key() {
  echo "sigs.k8s.io/cluster-api-provider-aws/cluster/$(_get_cluster_name "$1")"
}

# RDS instance identifier created by the ACK RDS controller on each workload
# cluster: krops-${CLUSTER_NAME}-db (see workload/base/rds-instances/dbinstance.yaml
# and the cluster-vars ConfigMap in mgmt/aws/addons/flux-apps/flux-instance.yaml).
_get_rds_instance() {
  case "$1" in
    eu-north-1) echo "krops-eu-north-1-workload-db" ;;
    eu-west-1)  echo "krops-eu-west-1-workload-db" ;;
    *)          return 1 ;;
  esac
}

# ── AWS orphan cleanup helpers ────────────────────────────────────────────────
# All helpers gracefully skip if the resource is already gone and never abort
# the script on failure.

# ── Pod identity associations ──────────────────────────────────────────────────
_cleanup_pod_identity_associations() {
  _region="$1"; _cluster="$2"

  # Only possible while the EKS cluster exists
  if ! aws eks describe-cluster --name "$_cluster" --region "$_region" \
       >/dev/null 2>&1; then
    success "EKS cluster $_cluster not found in $_region – no pod identity associations to clean"
    return 0
  fi

  for _assoc_id in $(aws eks list-pod-identity-associations \
      --cluster-name "$_cluster" --region "$_region" \
      --query 'associations[].associationId' --output text 2>/dev/null || true); do
    info "  Deleting pod identity association: $_assoc_id"
    aws eks delete-pod-identity-association \
      --cluster-name "$_cluster" --association-id "$_assoc_id" --region "$_region" \
      >/dev/null 2>&1 || warn "  Failed to delete pod identity association $_assoc_id"
  done
}

# ── Nodegroups ─────────────────────────────────────────────────────────────────
_cleanup_nodegroups() {
  _region="$1"; _cluster="$2"

  if ! aws eks describe-cluster --name "$_cluster" --region "$_region" \
       >/dev/null 2>&1; then
    return 0
  fi

  for _ng in $(aws eks list-nodegroups \
      --cluster-name "$_cluster" --region "$_region" \
      --query 'nodegroups[]' --output text 2>/dev/null || true); do
    info "  Deleting nodegroup: $_ng"
    aws eks delete-nodegroup \
      --cluster-name "$_cluster" --nodegroup-name "$_ng" --region "$_region" \
      >/dev/null 2>&1 || warn "  Failed to delete nodegroup $_ng"
  done
}

# EKS refuses to delete a cluster while nodegroups still exist, so we must
# wait for every nodegroup (including ones already mid-deletion) to be gone.
_wait_nodegroups_deleted() {
  _region="$1"; _cluster="$2"

  if ! aws eks describe-cluster --name "$_cluster" --region "$_region" \
       >/dev/null 2>&1; then
    return 0
  fi

  for _ng in $(aws eks list-nodegroups \
      --cluster-name "$_cluster" --region "$_region" \
      --query 'nodegroups[]' --output text 2>/dev/null || true); do
    info "  Waiting for nodegroup $_ng in $_region to finish deleting..."
    aws eks wait nodegroup-deleted \
      --cluster-name "$_cluster" --nodegroup-name "$_ng" --region "$_region" \
      2>/dev/null || warn "  Timed out waiting for nodegroup $_ng in $_region"
  done
}

# ── EKS cluster ────────────────────────────────────────────────────────────────
_cleanup_eks_cluster() {
  _region="$1"; _cluster="$2"

  if ! aws eks describe-cluster --name "$_cluster" --region "$_region" \
       >/dev/null 2>&1; then
    success "EKS cluster $_cluster not found in $_region"
    return 0
  fi

  info "  Deleting EKS cluster: $_cluster in $_region"
  aws eks delete-cluster --name "$_cluster" --region "$_region" \
    >/dev/null 2>&1 || warn "  Failed to delete EKS cluster $_cluster"
}

# The control plane's ENIs occupy the cluster subnets, so VPC cleanup can only
# succeed after the EKS cluster is fully gone.
_wait_eks_cluster_deleted() {
  _region="$1"; _cluster="$2"

  if ! aws eks describe-cluster --name "$_cluster" --region "$_region" \
       >/dev/null 2>&1; then
    return 0
  fi

  info "  Waiting for EKS cluster $_cluster in $_region to finish deleting..."
  aws eks wait cluster-deleted --name "$_cluster" --region "$_region" \
    2>/dev/null || warn "  Timed out waiting for EKS cluster $_cluster in $_region"
}

# ── RDS instances (created by the ACK RDS controller on workload clusters) ─────
# The DBInstance CRs live on the workload clusters, so when the clusters are
# deleted the ACK RDS controller is destroyed before it can deprovision the
# database. Delete the instances directly. The RDS-managed master password
# secret in Secrets Manager (manageMasterUserPassword) is removed by RDS
# automatically when the instance is deleted.
_cleanup_rds_instance() {
  _rds_region="$1"; _rds_db="$2"

  if ! aws rds describe-db-instances --db-instance-identifier "$_rds_db" \
       --region "$_rds_region" >/dev/null 2>&1; then
    success "RDS instance $_rds_db not found in $_rds_region"
    return 0
  fi

  _rds_status=$(aws rds describe-db-instances --db-instance-identifier "$_rds_db" \
    --query 'DBInstances[0].DBInstanceStatus' --output text \
    --region "$_rds_region" 2>/dev/null || true)
  if [ "$_rds_status" = "deleting" ]; then
    info "  RDS instance $_rds_db already deleting in $_rds_region"
    return 0
  fi

  info "  Deleting RDS instance: $_rds_db in $_rds_region"
  aws rds delete-db-instance \
    --db-instance-identifier "$_rds_db" \
    --skip-final-snapshot \
    --delete-automated-backups \
    --region "$_rds_region" >/dev/null \
    2>/dev/null || warn "  Failed to delete RDS instance $_rds_db"
}

# ── S3 buckets (created by the ACK S3 controller on workload clusters) ─────────
# Like the RDS instances, the Bucket CRs live on the workload clusters, so the
# buckets are orphaned when the clusters are deleted. Buckets are versioned
# (see workload/base/s3-buckets/bucket.yaml), so every object version AND delete
# marker must be purged before the bucket itself can be deleted.
_cleanup_s3_bucket() {
  _bucket="$1"; _bucket_region="$2"

  if ! aws s3api head-bucket --bucket "$_bucket" --region "$_bucket_region" \
       >/dev/null 2>&1; then
    success "S3 bucket $_bucket not found"
    return 0
  fi

  info "  Emptying S3 bucket: $_bucket (all versions and delete markers)"
  while :; do
    # JMESPath flatten drops nulls, so this handles Versions and/or
    # DeleteMarkers being absent. Output is the exact JSON shape that
    # delete-objects expects. Batches of 500 (delete-objects max is 1000).
    _batch=$(aws s3api list-object-versions --bucket "$_bucket" \
      --region "$_bucket_region" --max-items 500 \
      --query '{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}, Quiet: `true`}' \
      --output json 2>/dev/null) || break
    case "$_batch" in
      *'"Key"'*) ;;
      *) break ;;
    esac
    aws s3api delete-objects --bucket "$_bucket" --region "$_bucket_region" \
      --delete "$_batch" >/dev/null 2>&1 \
      || { warn "  Failed to purge objects from $_bucket"; break; }
  done

  info "  Deleting S3 bucket: $_bucket"
  aws s3api delete-bucket --bucket "$_bucket" --region "$_bucket_region" \
    2>/dev/null || warn "  Failed to delete S3 bucket $_bucket"
}

# ── VPC resources (krops only – gated on CAPA ownership tag) ─────────────────
_cleanup_vpc_resources() {
  _region="$1"; _cluster="$2"
  _cluster_tag_key=$(_get_capa_tag_key "$_region")

  for _vpc_id in $(aws ec2 describe-vpcs \
      --filter "Name=tag:${_cluster_tag_key},Values=owned" \
      --query 'Vpcs[].VpcId' --output text \
      --region "$_region" 2>/dev/null || true); do
    info "  Cleaning up VPC $_vpc_id in $_region (cluster: $_cluster)"

    # Deletion order inside VPC:
    #   1. NAT gateways (must go before subnets), then release their EIPs
    #   2. Subnets (non-default)
    #   3. Internet gateways (detach then delete)
    #   4. Route tables (non-main)
    #   5. Security groups (non-default)
    #   6. VPC

    _delete_nat_gateways "$_region" "$_vpc_id"
    _release_cluster_eips "$_region" "$_cluster_tag_key"
    _delete_subnets "$_region" "$_vpc_id"
    _delete_internet_gateways "$_region" "$_vpc_id"
    _delete_route_tables "$_region" "$_vpc_id"
    _delete_security_groups "$_region" "$_vpc_id"

    info "    Deleting VPC: $_vpc_id"
    aws ec2 delete-vpc --vpc-id "$_vpc_id" --region "$_region" \
      2>/dev/null || warn "    Failed to delete VPC $_vpc_id"
  done
}

_delete_nat_gateways() {
  _nat_region="$1"; _nat_vpc="$2"

  for _nat_id in $(aws ec2 describe-nat-gateways \
      --filter "Name=vpc-id,Values=$_nat_vpc" \
             "Name=state,Values=pending,available" \
      --query 'NatGateways[].NatGatewayId' --output text \
      --region "$_nat_region" 2>/dev/null || true); do
    info "    Deleting NAT gateway: $_nat_id"
    aws ec2 delete-nat-gateway --nat-gateway-id "$_nat_id" --region "$_nat_region" \
      2>/dev/null || warn "    Failed to delete NAT gateway $_nat_id"
  done

  # Wait for NAT gateways to reach DELETED state (subnets depend on this).
  # After delete-nat-gateway the state is "deleting", so the filter must
  # include it or this loop finds nothing and never waits.
  for _nat_id in $(aws ec2 describe-nat-gateways \
      --filter "Name=vpc-id,Values=$_nat_vpc" \
             "Name=state,Values=pending,available,deleting" \
      --query 'NatGateways[].NatGatewayId' --output text \
      --region "$_nat_region" 2>/dev/null || true); do
    info "    Waiting for NAT gateway $_nat_id to finish deleting..."
    aws ec2 wait nat-gateway-deleted \
      --nat-gateway-ids "$_nat_id" --region "$_nat_region" \
      2>/dev/null || warn "    Timeout waiting for NAT gateway $_nat_id"
  done
}

# Elastic IPs allocated by CAPA for the NAT gateways are not deleted with the
# gateway – they must be released explicitly or they linger (and cost money).
# Scoped to addresses carrying the CAPA cluster ownership tag.
_release_cluster_eips() {
  _eip_region="$1"; _eip_tag_key="$2"

  for _alloc_id in $(aws ec2 describe-addresses \
      --filter "Name=tag:${_eip_tag_key},Values=owned" \
      --query 'Addresses[].AllocationId' --output text \
      --region "$_eip_region" 2>/dev/null || true); do
    info "    Releasing Elastic IP: $_alloc_id"
    aws ec2 release-address --allocation-id "$_alloc_id" --region "$_eip_region" \
      2>/dev/null || warn "    Failed to release Elastic IP $_alloc_id"
  done
}

_delete_subnets() {
  _subnet_region="$1"; _subnet_vpc="$2"

  # No "non-default" filter needed: the enclosing VPC is CAPA-tagged, so it is
  # never the account's default VPC and every subnet in it belongs to krops.
  # ("default" is not a valid subnet filter name – using it makes the describe
  # call fail silently and skip subnet deletion entirely.)
  for _subnet_id in $(aws ec2 describe-subnets \
      --filter "Name=vpc-id,Values=$_subnet_vpc" \
      --query 'Subnets[].SubnetId' --output text \
      --region "$_subnet_region" 2>/dev/null || true); do
    info "    Deleting subnet: $_subnet_id"
    aws ec2 delete-subnet --subnet-id "$_subnet_id" --region "$_subnet_region" \
      2>/dev/null || warn "    Failed to delete subnet $_subnet_id"
  done
}

_delete_internet_gateways() {
  _igw_region="$1"; _igw_vpc="$2"

  for _igw_id in $(aws ec2 describe-internet-gateways \
      --filter "Name=attachment.vpc-id,Values=$_igw_vpc" \
      --query 'InternetGateways[].InternetGatewayId' --output text \
      --region "$_igw_region" 2>/dev/null || true); do
    info "    Detaching internet gateway: $_igw_id"
    aws ec2 detach-internet-gateway \
      --internet-gateway-id "$_igw_id" --vpc-id "$_igw_vpc" --region "$_igw_region" \
      2>/dev/null || true

    info "    Deleting internet gateway: $_igw_id"
    aws ec2 delete-internet-gateway --internet-gateway-id "$_igw_id" --region "$_igw_region" \
      2>/dev/null || warn "    Failed to delete IGW $_igw_id"
  done
}

_delete_route_tables() {
  _rt_region="$1"; _rt_vpc="$2"

  # Identify the main route table so we skip it
  _main_rt=$(aws ec2 describe-route-tables \
    --filter "Name=vpc-id,Values=$_rt_vpc" "Name=main,Values=true" \
    --query 'RouteTables[0].RouteTableId' --output text \
    --region "$_rt_region" 2>/dev/null || true)

  for _rt_id in $(aws ec2 describe-route-tables \
      --filter "Name=vpc-id,Values=$_rt_vpc" \
      --query 'RouteTables[].RouteTableId' --output text \
      --region "$_rt_region" 2>/dev/null || true); do
    [ "$_rt_id" = "$_main_rt" ] && continue

    info "    Deleting route table: $_rt_id"
    aws ec2 delete-route-table --route-table-id "$_rt_id" --region "$_rt_region" \
      2>/dev/null || warn "    Failed to delete route table $_rt_id"
  done
}

# EKS security groups reference each other, so a single delete pass fails
# with DependencyViolation. Pass 1 strips every ingress/egress rule from every
# non-default SG (revoke-* requires the actual rule set via --ip-permissions;
# calling it without rules is an error and revokes nothing). Pass 2 deletes.
_delete_security_groups() {
  _sg_region="$1"; _sg_vpc="$2"

  _sg_ids=""
  for _sg_id in $(aws ec2 describe-security-groups \
      --filter "Name=vpc-id,Values=$_sg_vpc" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text \
      --region "$_sg_region" 2>/dev/null || true); do
    _sg_ids="$_sg_ids $_sg_id"

    _ingress=$(aws ec2 describe-security-groups --group-ids "$_sg_id" \
      --query 'SecurityGroups[0].IpPermissions' --output json \
      --region "$_sg_region" 2>/dev/null || echo '[]')
    if [ "$_ingress" != "[]" ] && [ -n "$_ingress" ]; then
      aws ec2 revoke-security-group-ingress --group-id "$_sg_id" \
        --ip-permissions "$_ingress" --region "$_sg_region" \
        >/dev/null 2>&1 || true
    fi

    _egress=$(aws ec2 describe-security-groups --group-ids "$_sg_id" \
      --query 'SecurityGroups[0].IpPermissionsEgress' --output json \
      --region "$_sg_region" 2>/dev/null || echo '[]')
    if [ "$_egress" != "[]" ] && [ -n "$_egress" ]; then
      aws ec2 revoke-security-group-egress --group-id "$_sg_id" \
        --ip-permissions "$_egress" --region "$_sg_region" \
        >/dev/null 2>&1 || true
    fi
  done

  for _sg_id in $_sg_ids; do
    info "    Deleting security group: $_sg_id"
    aws ec2 delete-security-group --group-id "$_sg_id" --region "$_sg_region" \
      2>/dev/null || warn "    Failed to delete security group $_sg_id"
  done
}

# ── IAM role ───────────────────────────────────────────────────────────────────
_cleanup_iam_role() {
  _role="$1"

  if ! aws iam get-role --role-name "$_role" >/dev/null 2>&1; then
    return 0
  fi

  info "  Deleting IAM role: $_role"

  # Detach managed policies
  for _policy_arn in $(aws iam list-attached-role-policies --role-name "$_role" \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || true); do
    info "    Detaching policy: $_policy_arn"
    aws iam detach-role-policy --role-name "$_role" --policy-arn "$_policy_arn" \
      2>/dev/null || true
  done

  # Remove role from instance profiles and delete them
  for _profile in $(aws iam list-instance-profiles-for-role --role-name "$_role" \
      --query 'InstanceProfiles[].InstanceProfileName' --output text 2>/dev/null || true); do
    info "    Deleting instance profile: $_profile"
    aws iam remove-role-from-instance-profile \
      --instance-profile-name "$_profile" --role-name "$_role" \
      2>/dev/null || true
    aws iam delete-instance-profile --instance-profile-name "$_profile" \
      2>/dev/null || true
  done

  # Delete inline policies
  for _policy_name in $(aws iam list-role-policies --role-name "$_role" \
      --query 'PolicyNames[]' --output text 2>/dev/null || true); do
    info "    Deleting inline policy: $_policy_name"
    aws iam delete-role-policy --role-name "$_role" --policy-name "$_policy_name" \
      2>/dev/null || true
  done

  # Delete the role itself
  aws iam delete-role --role-name "$_role" 2>/dev/null \
    || warn "  Failed to delete IAM role $_role"
}

# CAPA (EKSEnableIAM=true) auto-creates per-cluster IAM roles whose exact
# names are not declared in Git (e.g. <cluster>-iam-service-role and the
# nodegroup roles). Sweep every role whose name starts with the cluster name –
# that prefix ("eu-north-1-workload"/"eu-west-1-workload") is unique to this
# repo's clusters.
_cleanup_capa_iam_roles() {
  _cluster_name="$1"

  for _role in $(aws iam list-roles \
      --query "Roles[?starts_with(RoleName, \`${_cluster_name}\`)].RoleName" \
      --output text 2>/dev/null || true); do
    _cleanup_iam_role "$_role"
  done
}

# ── IAM user ───────────────────────────────────────────────────────────────────
# Users need different cleanup than roles: the login profile (console
# password, created imperatively via create-login-profile), any access keys,
# and inline policies must all be removed before the user can be deleted.
_cleanup_iam_user() {
  _user="$1"

  if ! aws iam get-user --user-name "$_user" >/dev/null 2>&1; then
    return 0
  fi

  info "  Deleting IAM user: $_user"

  # Delete the console login profile (may not exist)
  aws iam delete-login-profile --user-name "$_user" 2>/dev/null || true

  # Delete access keys (none are created by this repo, but be thorough)
  for _key_id in $(aws iam list-access-keys --user-name "$_user" \
      --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null || true); do
    info "    Deleting access key: $_key_id"
    aws iam delete-access-key --user-name "$_user" --access-key-id "$_key_id" \
      2>/dev/null || true
  done

  # Delete inline policies
  for _policy_name in $(aws iam list-user-policies --user-name "$_user" \
      --query 'PolicyNames[]' --output text 2>/dev/null || true); do
    info "    Deleting inline policy: $_policy_name"
    aws iam delete-user-policy --user-name "$_user" --policy-name "$_policy_name" \
      2>/dev/null || true
  done

  # Delete the user itself
  aws iam delete-user --user-name "$_user" 2>/dev/null \
    || warn "  Failed to delete IAM user $_user"
}

# ── CloudFormation stack ───────────────────────────────────────────────────────
_cleanup_cfn_stack() {
  _cfn_region="$1"; _cfn_stack="$2"

  if ! aws cloudformation describe-stacks --stack-name "$_cfn_stack" \
       --region "$_cfn_region" >/dev/null 2>&1; then
    return 0
  fi

  info "  Deleting CFN stack: $_cfn_stack in $_cfn_region"
  aws cloudformation delete-stack --stack-name "$_cfn_stack" --region "$_cfn_region" \
    2>/dev/null || warn "  Failed to delete CFN stack $_cfn_stack in $_cfn_region"
}

# ── Prerequisites: management cluster check ───────────────────────────────────
if [ "${AWS_ONLY:-0}" != "1" ]; then
  # Verify the management cluster is reachable before proceeding.
  if ! kubectl cluster-info --request-timeout=10s >/dev/null 2>&1; then
    warn "Cannot reach the management cluster. It may already be gone."
    warn "Running AWS orphan cleanup only. To skip this warning, set AWS_ONLY=1."
    echo ""
  fi
fi

# ── Step 1: Suspend Flux reconciliation ───────────────────────────────────────
# Prevents Flux from re-applying manifests while we tear resources down.
if [ "${AWS_ONLY:-0}" != "1" ]; then
  info "Suspending Flux Kustomizations to prevent re-reconciliation..."
  if kubectl get kustomizations.kustomize.toolkit.fluxcd.io -n flux-system >/dev/null 2>&1; then
    kubectl get kustomizations.kustomize.toolkit.fluxcd.io -n flux-system \
      -o name 2>/dev/null \
      | xargs --no-run-if-empty kubectl patch \
          -n flux-system --type=merge \
          -p '{"spec":{"suspend":true}}' \
      || warn "Could not suspend some Kustomizations – continuing anyway"
    success "Flux Kustomizations suspended"
  else
    warn "No Flux Kustomizations found – skipping suspension"
  fi
else
  warn "AWS_ONLY mode – skipping Flux suspension"
fi

# ── Step 2: Delete CAPI workload clusters ─────────────────────────────────────
# Deleting a CAPI Cluster resource triggers CAPA to destroy all associated AWS
# resources (VPC, subnets, NAT gateways, EKS cluster, managed node groups).
if [ "${AWS_ONLY:-0}" != "1" ]; then
  info "Discovering CAPI Cluster resources..."
  if kubectl api-resources --api-group=cluster.x-k8s.io >/dev/null 2>&1; then
    CLUSTERS=$(kubectl get clusters.cluster.x-k8s.io -A \
      -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null || true)

    if [ -n "$CLUSTERS" ]; then
      info "Deleting CAPI workload clusters (AWS teardown will begin in each region)..."
      echo "$CLUSTERS" | while IFS='/' read -r ns name; do
        info "  Deleting cluster: $name (namespace: $ns)"
        kubectl delete cluster.cluster.x-k8s.io "$name" -n "$ns" --ignore-not-found
      done

      # ── Step 3: Wait for CAPI clusters to be deprovisioned ──────────────────
      # CAPA must finish deleting VPCs, EKS clusters, node groups, etc. before we
      # destroy the management cluster — otherwise the CAPA controller is gone and
      # AWS resources are orphaned.
      info "Waiting up to ${CLUSTER_DELETE_TIMEOUT}s for all CAPI clusters to be deleted..."
      info "(This typically takes 15–25 minutes while CAPA tears down AWS resources)"
      ELAPSED=0
      INTERVAL=30
      while true; do
        REMAINING=$(kubectl get clusters.cluster.x-k8s.io -A \
          --no-headers 2>/dev/null | wc -l | tr -d ' ')
        if [ "$REMAINING" -eq 0 ]; then
          success "All CAPI clusters deleted"
          CLUSTERS_CONFIRMED_GONE=1
          break
        fi
        if [ "$ELAPSED" -ge "$CLUSTER_DELETE_TIMEOUT" ]; then
          warn "Timed out waiting for CAPI clusters to be deleted after ${ELAPSED}s"
          warn "The following clusters still exist:"
          kubectl get clusters.cluster.x-k8s.io -A --no-headers 2>/dev/null || true
          warn ""
          warn "ABORTING teardown. The management cluster and CAPA controller have been"
          warn "left intact so AWS resources can continue to deprovision. Re-run this"
          warn "script once 'kubectl get clusters -A' is empty."
          warn "(To force-delete the management cluster and accept orphaned AWS"
          warn "resources, re-run with FORCE_KIND_DELETE=1.)"
          # CLUSTERS_CONFIRMED_GONE stays 0, so the EXIT trap leaves kind running.
          exit 1
        fi
        info "  ${REMAINING} cluster(s) still deleting... (${ELAPSED}s elapsed, checking again in ${INTERVAL}s)"
        sleep "$INTERVAL"
        ELAPSED=$((ELAPSED + INTERVAL))
      done
    else
      info "No CAPI clusters found – skipping cluster deletion"
      CLUSTERS_CONFIRMED_GONE=1
    fi
  else
    warn "CAPI CRDs not present – skipping cluster deletion"
    CLUSTERS_CONFIRMED_GONE=1
  fi
else
  warn "AWS_ONLY mode – skipping CAPI cluster deletion"
  CLUSTERS_CONFIRMED_GONE=1
fi

# ── Step 4: AWS orphan cleanup ────────────────────────────────────────────────
# Cleans up resources that CAPA/CAPI teardown does not manage or may leave
# behind. Each sub-step is idempotent and skips gracefully if the resource is
# already gone.
#
# Sub-steps (each runs across ALL regions in $REGIONS before moving on, so
# both regions' slow deletions overlap instead of blocking each other):
#   4a. Pod identity associations  – ACK controllers (EKS API, needs cluster)
#   4b. Nodegroups                 – delete in both regions, then wait: EKS
#                                   refuses to delete a cluster with nodegroups
#   4c. EKS clusters               – delete in both regions, then wait: the
#                                   control plane ENIs block VPC cleanup
#   4d. RDS instances              – ACK-created DBInstances (orphaned when the
#                                   workload cluster dies before the CR prunes)
#   4e. VPC resources              – subnets, IGW, NAT+EIPs, route tables, SGs,
#                                   VPC – scoped to CAPA-tagged VPCs only
#   4f. S3 buckets                 – ACK-created versioned data buckets
#   4g. IAM roles + users          – CAPA per-cluster roles (prefix sweep)
#                                   + ACK controller roles
#                                   + ACK-created krops-*-reader roles
#                                   + the krops-reader console user
#   4h. CloudFormation stack       – clusterawsadm bootstrap stack

step_aws_cleanup() {
  info "Cleaning up orphaned AWS resources in regions: $REGIONS"

  # ── 4a+4b: pod identity associations, then kick off nodegroup deletion ─────
  for _region in $REGIONS; do
    _eks_cluster=$(_get_eks_cluster "$_region")
    info "  [$_region] cluster: $_eks_cluster"

    _cleanup_pod_identity_associations "$_region" "$_eks_cluster"
    _cleanup_nodegroups "$_region" "$_eks_cluster"
  done

  # Nodegroup deletions in both regions are now running; wait for all of them.
  for _region in $REGIONS; do
    _wait_nodegroups_deleted "$_region" "$(_get_eks_cluster "$_region")"
  done

  # ── 4c: EKS clusters – delete in both regions, then wait for both ──────────
  for _region in $REGIONS; do
    _cleanup_eks_cluster "$_region" "$(_get_eks_cluster "$_region")"
  done
  for _region in $REGIONS; do
    _wait_eks_cluster_deleted "$_region" "$(_get_eks_cluster "$_region")"
  done

  # ── 4d: RDS instances (ACK-created, live in each region's default VPC) ─────
  for _region in $REGIONS; do
    _cleanup_rds_instance "$_region" "$(_get_rds_instance "$_region")"
  done

  # ── 4e: VPC resources (CAPA-tagged only – krops scope) ───────────────────
  for _region in $REGIONS; do
    _cleanup_vpc_resources "$_region" "$(_get_cluster_name "$_region")"
  done

  # ── 4f: S3 buckets (krops-${ACCOUNT_ID}-${CLUSTER_NAME}-data) ────────────
  _account_id=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
  if [ -n "$_account_id" ]; then
    for _region in $REGIONS; do
      _cleanup_s3_bucket "krops-${_account_id}-$(_get_cluster_name "$_region")-data" "$_region"
    done
  else
    warn "  Could not determine AWS account ID – skipping S3 bucket cleanup"
  fi

  # ── 4g: IAM roles + users ───────────────────────────────────────────────────
  for _region in $REGIONS; do
    _cleanup_capa_iam_roles "$(_get_cluster_name "$_region")"
  done
  for _role in $GLOBAL_IAM_ROLES; do
    _cleanup_iam_role "$_role"
  done
  for _user in $GLOBAL_IAM_USERS; do
    _cleanup_iam_user "$_user"
  done

  # ── 4h: CloudFormation stack ────────────────────────────────────────────────
  for _region in $REGIONS; do
    _cleanup_cfn_stack "$_region" "$CFN_STACK_NAME"
  done

  success "AWS orphan cleanup complete"
}

if [ "$AWS_AVAILABLE" = "true" ]; then
  step_aws_cleanup
else
  warn "aws CLI not available – skipping AWS orphan cleanup (step 4)"
fi

# ── Step 5: Delete CAPI providers ─────────────────────────────────────────────
# Removing the provider CRs causes the CAPI Operator to uninstall the provider
# controllers and their associated namespaces.
if [ "${AWS_ONLY:-0}" != "1" ]; then
  info "Deleting CAPI providers..."
  if kubectl api-resources --api-group=operator.cluster.x-k8s.io >/dev/null 2>&1; then
    for kind in addonproviders controlplaneproviders bootstrapproviders infrastructureproviders coreproviders; do
      ITEMS=$(kubectl get "$kind.operator.cluster.x-k8s.io" -A \
        -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
      if [ -n "$ITEMS" ]; then
        echo "$ITEMS" | while IFS='/' read -r ns name; do
          info "  Deleting $kind: $name (namespace: $ns)"
          kubectl delete "$kind.operator.cluster.x-k8s.io" "$name" -n "$ns" --ignore-not-found
        done
      fi
    done

    info "Waiting up to ${PROVIDER_DELETE_TIMEOUT}s for CAPI providers to be removed..."
    ELAPSED=0
    INTERVAL=15
    while true; do
      REMAINING=0
      for kind in addonproviders controlplaneproviders bootstrapproviders infrastructureproviders coreproviders; do
        COUNT=$(kubectl get "$kind.operator.cluster.x-k8s.io" -A \
          --no-headers 2>/dev/null | wc -l | tr -d ' ')
        REMAINING=$((REMAINING + COUNT))
      done
      if [ "$REMAINING" -eq 0 ]; then
        success "All CAPI providers removed"
        break
      fi
      if [ "$ELAPSED" -ge "$PROVIDER_DELETE_TIMEOUT" ]; then
        warn "Timed out waiting for CAPI providers to be removed – continuing anyway"
        break
      fi
      info "  ${REMAINING} provider(s) still removing... (${ELAPSED}s elapsed)"
      sleep "$INTERVAL"
      ELAPSED=$((ELAPSED + INTERVAL))
    done
  else
    warn "CAPI Operator CRDs not present – skipping provider deletion"
  fi
else
  warn "AWS_ONLY mode – skipping CAPI provider deletion"
fi

# ── Step 6: Uninstall FluxInstance Helm release ───────────────────────────────
if [ "${AWS_ONLY:-0}" != "1" ]; then
  info "Uninstalling FluxInstance Helm release..."
  if helm status flux -n flux-system >/dev/null 2>&1; then
    helm uninstall flux --namespace flux-system --wait --timeout 5m0s
    success "FluxInstance Helm release uninstalled"
  else
    warn "FluxInstance Helm release 'flux' not found – skipping"
  fi
else
  warn "AWS_ONLY mode – skipping FluxInstance uninstall"
fi

# ── Step 7: Uninstall Flux Operator Helm release ──────────────────────────────
if [ "${AWS_ONLY:-0}" != "1" ]; then
  info "Uninstalling Flux Operator Helm release..."
  if helm status flux-operator -n flux-system >/dev/null 2>&1; then
    helm uninstall flux-operator --namespace flux-system --wait --timeout 5m0s
    success "Flux Operator Helm release uninstalled"
  else
    warn "Flux Operator Helm release 'flux-operator' not found – skipping"
  fi
else
  warn "AWS_ONLY mode – skipping Flux Operator uninstall"
fi

# ── Step 8: Delete secrets ────────────────────────────────────────────────────
if [ "${AWS_ONLY:-0}" != "1" ]; then
  info "Deleting GitHub PAT and SOPS age secrets..."
  kubectl delete secret flux-github-pat -n flux-system --ignore-not-found
  kubectl delete secret sops-age -n flux-system --ignore-not-found
  # The aws-credentials secret is GitOps-managed (decrypted by Flux), but remove
  # it explicitly in case Flux was uninstalled before it could be pruned.
  kubectl delete secret aws-credentials -n capa-system --ignore-not-found
  success "Secrets deleted (or were already absent)"
else
  warn "AWS_ONLY mode – skipping secret deletion"
fi

# ── Step 9: Delete the kind management cluster ────────────────────────────────
# Cluster-aware: only deletes kind once CLUSTERS_CONFIRMED_GONE=1 (or when
# FORCE_KIND_DELETE=1). Disarm the EXIT trap first so it doesn't double-fire.
trap - EXIT
if [ "${AWS_ONLY:-0}" != "1" ]; then
  _delete_kind_if_safe
else
  warn "AWS_ONLY mode – skipping kind cluster deletion"
fi

echo ""
echo "✓ Teardown complete."
