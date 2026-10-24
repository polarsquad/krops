#!/usr/bin/env bash
# update-package.sh: staged, verified, transactional update of the air-gap Zarf
# package archive (archive-level rollback only; see docs/airgap.md).
#
# Disk model for `apply` (docs/airgap.md#updating-a-deployed-bundle):
# peak = active + .staging + one zarf verify extraction (a full unpack of the
# package, 1.7-2.5 GiB for the current 1.6 GiB package). verify-package.sh
# keeps the extraction under a sibling of the SBOM dir (zarf --tmpdir) and
# removes it on every exit path, so only one extraction is live at a time.
# Budget that peak on the filesystem holding archives/.
#
# Usage: update-package.sh apply <package> | confirm | rollback | status
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
AIRGAP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
ARCHIVES="$AIRGAP_DIR/archives"

# Manually maintained: keep in sync with airgap/zarf.yaml metadata.version and
# the offline-run.sh PACKAGE_INPUT default; the naming-invariant test enforces
# the offline-run.sh sync.
ACTIVE_NAME=zarf-package-krops-airgap-arm64-0.1.0.tar.zst
ACTIVE=$ARCHIVES/$ACTIVE_NAME
BACKUP=$ACTIVE.bak
BACKUP_SUM=$BACKUP.sha256
STAGING=$ARCHIVES/.staging
STATE=$ARCHIVES/.update-state
LOCK=$ARCHIVES/.update.lock

# shellcheck source=verify-package.sh
source "$SCRIPT_DIR/verify-package.sh"

usage() {
  echo "usage: $(basename "$0") apply <package> | confirm | rollback | status" >&2
  exit 64
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

write_state() {
  printf '%b\n' "$1" > "$STATE.tmp"
  mv -f "$STATE.tmp" "$STATE"
}

state_phase() {
  sed -n 's/^phase=//p' "$STATE" | head -n 1
}

state_is_first_install() {
  grep -qx 'first_install=1' "$STATE" 2>/dev/null
}

release_lock() {
  if [ "$(cat "$LOCK/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -rf "$LOCK"
  fi
}

acquire_lock() {
  if ! mkdir "$LOCK" 2>/dev/null; then
    local holder
    holder=$(cat "$LOCK/pid" 2>/dev/null || true)
    if [ -z "$holder" ]; then
      echo "update-package.sh: $LOCK exists without a pid; remove it if no update is running" >&2
      exit 1
    fi
    if kill -0 "$holder" 2>/dev/null; then
      echo "update-package.sh: another update is running (pid $holder)" >&2
      exit 1
    fi
    echo "update-package.sh: removing stale lock left by pid $holder" >&2
    # Not race-free: two concurrent reclaims can both win; run one update at a time.
    rm -rf "$LOCK"
    mkdir "$LOCK"
  fi
  echo $$ > "$LOCK/pid"
  trap release_lock EXIT
}

require_state() {
  if [ ! -f "$STATE" ]; then
    echo "update-package.sh: no update in progress (no $STATE)" >&2
    exit 1
  fi
}

status() {
  if [ -f "$ACTIVE" ]; then
    echo "active: $ACTIVE_NAME sha256=$(sha256_of "$ACTIVE")"
  else
    echo "active: none"
  fi
  if [ -f "$BACKUP" ]; then
    echo "backup: $BACKUP recorded sha256=$(cat "$BACKUP_SUM" 2>/dev/null || echo unknown)"
  else
    echo "backup: none"
  fi
  if [ -f "$STATE" ]; then
    echo "state: $(tr '\n' ' ' < "$STATE")"
  else
    echo "state: none"
  fi
  if [ -d "$STAGING" ]; then
    echo "staging: leftover $STAGING (discarded by the next apply)"
  fi
}

restore_previous() {
  if state_is_first_install; then
    rm -f "$ACTIVE" "$STATE"
    echo "first install failed; no prior archive to restore"
    return 0
  fi
  if [ ! -f "$BACKUP" ] || [ "$(sha256_of "$BACKUP")" != "$(cat "$BACKUP_SUM" 2>/dev/null || true)" ]; then
    echo "backup integrity check failed; manual recovery needed" >&2
    exit 4
  fi
  mv -f "$BACKUP" "$ACTIVE"
  rm -f "$BACKUP_SUM" "$STATE"
  echo "prior archive restored; cluster state was NOT rolled back; redeploy with offline-run.sh to restore cluster"
}

apply() {
  local package=$1 sbom_dir

  [ -f "$package" ] || { echo "update-package.sh: package not found: $package" >&2; exit 1; }
  acquire_lock

  if [ -f "$STATE" ]; then
    case "$(state_phase)" in
      cutover|awaiting-confirm)
        status
        echo "update-package.sh: prior update not finished; confirm or roll back first" >&2
        exit 1
        ;;
      *)
        echo "update-package.sh: unexpected state: $(tr '\n' ' ' < "$STATE"); inspect and roll back or remove $STATE manually" >&2
        exit 1
        ;;
    esac
  fi

  rm -rf "$STAGING"
  mkdir "$STAGING"
  cp "$package" "$STAGING/$ACTIVE_NAME"
  sync "$STAGING/$ACTIVE_NAME"

  sbom_dir=$(mktemp -d "${TMPDIR:-/tmp}/airgap-update-sbom.XXXXXX")
  if ! airgap_verify_package "$STAGING/$ACTIVE_NAME" "$sbom_dir"; then
    rm -rf "$STAGING" "$sbom_dir"
    echo "update-package.sh: rejected during staging; active package untouched" >&2
    exit 2
  fi
  rm -rf "$sbom_dir"

  local first_install=0
  if [ -e "$ACTIVE" ]; then
    ln "$ACTIVE" "$BACKUP" 2>/dev/null || cp -p "$ACTIVE" "$BACKUP"
    sha256_of "$BACKUP" > "$BACKUP_SUM"
    write_state "phase=cutover"
  else
    first_install=1
    write_state "phase=cutover\nfirst_install=1"
  fi

  mv -f "$STAGING/$ACTIVE_NAME" "$ACTIVE"
  rm -rf "$STAGING"

  if ! "${AIRGAP_DEPLOY_CMD:-$SCRIPT_DIR/offline-run.sh}" "$ACTIVE"; then
    echo "update-package.sh: deploy failed; restoring prior archive" >&2
    restore_previous
    exit 3
  fi

  # first_install must survive into awaiting-confirm: rollback of a first
  # install has no .bak to restore and relies on this marker.
  if [ "$first_install" -eq 1 ]; then
    write_state "phase=awaiting-confirm\nfirst_install=1"
  else
    write_state "phase=awaiting-confirm"
  fi
  echo "update-package.sh: deployed; run 'update-package.sh confirm' or 'update-package.sh rollback'"
}

rollback() {
  acquire_lock
  require_state

  if state_is_first_install; then
    rm -f "$ACTIVE" "$STATE"
    echo "first install rolled back; no prior archive"
    exit 0
  fi
  if [ ! -f "$BACKUP" ] || [ "$(sha256_of "$BACKUP")" != "$(cat "$BACKUP_SUM" 2>/dev/null || true)" ]; then
    echo "update-package.sh: backup integrity check failed; manual recovery needed" >&2
    exit 4
  fi
  mv -f "$BACKUP" "$ACTIVE"
  rm -f "$BACKUP_SUM" "$STATE"
  echo "rollback complete; cluster state was NOT rolled back; redeploy with offline-run.sh to restore cluster"
}

confirm() {
  acquire_lock
  require_state

  case "$(state_phase)" in
    cutover)
      echo "update-package.sh: deploy may not have completed; use rollback or apply again" >&2
      exit 1
      ;;
    awaiting-confirm) ;;
    *)
      echo "update-package.sh: unexpected state: $(tr '\n' ' ' < "$STATE")" >&2
      exit 1
      ;;
  esac
  rm -f "$BACKUP" "$BACKUP_SUM" "$STATE"
  echo "confirmed; prior archive discarded"
}

[ $# -ge 1 ] || usage
case "$1" in
  apply)
    [ $# -eq 2 ] || usage
    apply "$2"
    ;;
  rollback|confirm)
    [ $# -eq 1 ] || usage
    "$1"
    ;;
  status)
    [ $# -eq 1 ] || usage
    status
    ;;
  *)
    usage
    ;;
esac
