#!/usr/bin/env bash
# Eventually backfill the four lifecycle tags on current EKS managed workers.
# AWS CLI paginates discovery automatically; never pass --no-paginate.
set -uo pipefail
source "$(dirname "$0")/config.env"
export AWS_PAGER="" AWS_EC2_METADATA_DISABLED=true AWS_RETRY_MODE=standard AWS_MAX_ATTEMPTS=3
status=0
errors="$(mktemp)" || exit 1
data="$(mktemp)" || { rm -f "$errors"; exit 1; }
trap 'rm -f "$errors" "$data"' EXIT
helper="$(dirname "$0")/tag_data.py"
fail() { echo "ERROR: $*" >&2; status=1; }

account="$(aws sts get-caller-identity --query Account --output text)" || exit 1
if [ "$account" != "$EXPECTED_ACCOUNT" ] || [ "$EXPECTED_ACCOUNT" != 120392301094 ]; then
  echo 'ERROR: refusing worker tagging outside the designated account' >&2
  exit 1
fi

reconcile_cluster() {
  local region="$1" cluster="$2" tags run_id revision expires_at run_kind tag_json
  local groups group asgs asg instances instance members row id state owner current_run current_revision current_expiry current_kind
  local seen=' '
  case "$region:$cluster" in
    eu-north-1:default_eu-north-1-management-control-plane|eu-north-1:default_eu-north-1-workload-control-plane|eu-west-1:default_eu-west-1-workload-control-plane) ;;
    *) fail "refusing non-allowlisted cluster $region:$cluster"; return ;;
  esac
  tags="$(aws eks describe-cluster --region "$region" --name "$cluster" \
    --output json)" \
    || { fail "$cluster: reading cluster tags"; return; }
  if ! printf '%s' "$tags" | python3 "$helper" cluster >"$data"; then
    fail "$cluster: missing or malformed lifecycle tags"; return
  fi
  {
    IFS= read -r -d '' run_id
    IFS= read -r -d '' revision
    IFS= read -r -d '' expires_at
    IFS= read -r -d '' run_kind
    IFS= read -r -d '' tag_json
  } <"$data" || { fail "$cluster: reading validated tag fields"; return; }
  groups="$(aws eks list-nodegroups --region "$region" --cluster-name "$cluster" --query 'nodegroups[]' --output text)" \
    || { fail "$cluster: listing nodegroups"; return; }
  for group in $groups; do
    if [[ ! "$group" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then fail "$cluster: invalid nodegroup"; continue; fi
    asgs="$(aws eks describe-nodegroup --region "$region" --cluster-name "$cluster" --nodegroup-name "$group" \
      --query 'nodegroup.resources.autoScalingGroups[].name' --output text)" \
      || { fail "$cluster/$group: describing nodegroup"; continue; }
    for asg in $asgs; do
      if [[ ! "$asg" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$ ]]; then fail "$cluster: invalid ASG"; continue; fi
      instances="$(aws autoscaling describe-auto-scaling-groups --region "$region" --auto-scaling-group-names "$asg" \
        --query 'AutoScalingGroups[].Instances[].InstanceId' --output text)" \
        || { fail "$cluster/$asg: discovering workers"; continue; }
      for instance in $instances; do
        if [[ ! "$instance" =~ ^i-([0-9a-f]{8}|[0-9a-f]{17})$ ]]; then fail "$cluster: invalid instance ID"; continue; fi
        case "$seen" in *" $instance "*) continue ;; esac
        seen="$seen$instance "
        # Recheck membership immediately before fetching the instance's ownership.
        members="$(aws autoscaling describe-auto-scaling-groups --region "$region" --auto-scaling-group-names "$asg" \
          --query 'AutoScalingGroups[].Instances[].InstanceId' --output text)" \
          || { fail "$cluster/$instance: rechecking ASG membership"; continue; }
        if ! [[ " ${members//$'\t'/ } " =~ (^|[[:space:]])$instance([[:space:]]|$) ]]; then continue; fi
        row="$(aws ec2 describe-instances --region "$region" --instance-ids "$instance" \
          --output json 2>"$errors")" || {
            if ! grep -q 'InvalidInstanceID.NotFound' "$errors"; then fail "$cluster/$instance: describing worker"; fi
            continue
          }
        if ! printf '%s' "$row" | python3 "$helper" instance >"$data"; then
          fail "$cluster/$instance: malformed instance data"; continue
        fi
        [ -s "$data" ] || continue
        {
          IFS= read -r -d '' id
          IFS= read -r -d '' state
          IFS= read -r -d '' owner
          IFS= read -r -d '' current_run
          IFS= read -r -d '' current_revision
          IFS= read -r -d '' current_expiry
          IFS= read -r -d '' current_kind
        } <"$data" || { fail "$cluster/$instance: reading instance fields"; continue; }
        if [ "$state" = terminated ] || [ "$state" = shutting-down ]; then continue; fi
        if [ "$id" != "$instance" ] || [ "$owner" != "$cluster" ] \
          || [[ ! "$state" =~ ^(pending|running|stopping|stopped)$ ]]; then
          fail "$cluster/$instance: ownership or instance response mismatch"; continue
        fi
        if [ "$current_run" = "$run_id" ] && [ "$current_revision" = "$revision" ] \
          && [ "$current_expiry" = "$expires_at" ] && [ "$current_kind" = "$run_kind" ]; then continue; fi
        aws ec2 create-tags --region "$region" --resources "$instance" --tags "$tag_json" \
          || { fail "$cluster/$instance: writing lifecycle tags"; continue; }
        echo "Tagged $region/$cluster/$instance"
      done
    done
  done
}

for entry in $CLUSTERS; do
  reconcile_cluster "${entry%%:*}" "${entry#*:}"
done
exit "$status"
