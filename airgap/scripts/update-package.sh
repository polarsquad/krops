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
STATE_NEXT=$ARCHIVES/.update-state.next
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
  printf '%b\n' "$1" > "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"
}

# Free KiB on the filesystem holding $1: empty when df gives no number, 0 when negative.
free_kb() {
  df -Pk "$1" | awk 'NR == 2 {
    for (i = 2; i <= NF; i++) if ($i ~ /%$/) { v = $(i - 1); break }
    if (v ~ /^-[0-9]+$/) v = 0
    print v
  }'
}

# Device name of the filesystem holding $1 (a heuristic for "same filesystem").
fs_dev() {
  df -Pk "$1" | awk 'NR == 2 {print $1}'
}

is_number() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
  esac
}

state_phase() {
  sed -n 's/^phase=//p' "$STATE" | head -n 1
}

state_is_first_install() {
  grep -qx 'first_install=1' "$STATE" 2>/dev/null
}

# The lock is a symlink whose target is the pid. Creating a symlink needs no data
# block, so rollback and confirm still take the lock on a full filesystem.
lock_holder() {
  if [ -L "$LOCK" ]; then
    readlink "$LOCK"
  elif [ -d "$LOCK" ]; then
    # Lock directory from an older update-package.sh.
    cat "$LOCK/pid" 2>/dev/null || true
  fi
}

release_lock() {
  if [ -L "$LOCK" ]; then
    if [ "$(readlink "$LOCK")" = "$$" ]; then
      rm -f "$LOCK"
    fi
  elif [ -d "$LOCK" ] && [ "$(cat "$LOCK/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -rf "$LOCK"
  fi
}

# Symlink lock: no data block needed. Filesystems without symlinks (vfat, exFAT, some
# CIFS) get a directory with a pid file, which needs free space for that file.
take_lock() {
  if ln -s "$$" "$LOCK" 2>/dev/null; then
    if [ -L "$LOCK" ] && [ "$(readlink "$LOCK")" = "$$" ]; then
      trap release_lock EXIT
      return 0
    fi
    rm -f "$LOCK/$$"   # ln created a link inside an existing directory
    return 1
  fi
  mkdir "$LOCK" 2>/dev/null || return 1
  trap release_lock EXIT
  if ! echo "$$" > "$LOCK/pid"; then
    rm -rf "$LOCK"
    echo "update-package.sh: cannot write the pid file in $LOCK; nothing changed" >&2
    exit 1
  fi
}

acquire_lock() {
  local holder
  if [ ! -e "$LOCK" ] && [ ! -L "$LOCK" ]; then
    take_lock || { echo "update-package.sh: cannot create $LOCK; nothing changed" >&2; exit 1; }
    return 0
  fi
  holder=$(lock_holder)
  if [ -z "$holder" ]; then
    echo "update-package.sh: $LOCK has no readable owner; remove it if no update is running" >&2
    exit 1
  fi
  if kill -0 "$holder" 2>/dev/null; then
    echo "update-package.sh: another update is running (pid $holder)" >&2
    exit 1
  fi
  echo "update-package.sh: removing stale lock left by pid $holder" >&2
  # Not race-free: two concurrent reclaims can both win; run one update at a time.
  rm -rf "$LOCK"
  take_lock || { echo "update-package.sh: cannot create $LOCK; nothing changed" >&2; exit 1; }
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

# Puts the backup back as the active archive. A restore interrupted after the backup was
# consumed is finished here: the active archive then already holds the recorded copy.
restore_backup() {
  local recorded
  recorded=$(cat "$BACKUP_SUM" 2>/dev/null || true)
  if [ -f "$BACKUP" ] && [ "$(sha256_of "$BACKUP")" = "$recorded" ]; then
    # Before the swap, .bak is a second link to the active archive, which is still the prior one.
    if [ "$BACKUP" -ef "$ACTIVE" ]; then
      rm -f "$BACKUP"
    else
      mv -f "$BACKUP" "$ACTIVE"
    fi
  elif [ ! -e "$BACKUP" ] && [ -n "$recorded" ] && [ -f "$ACTIVE" ] && [ "$(sha256_of "$ACTIVE")" = "$recorded" ]; then
    :
  else
    echo "update-package.sh: backup integrity check failed; manual recovery needed" >&2
    exit 4
  fi
}

restore_previous() {
  if state_is_first_install; then
    rm -f "$ACTIVE" "$STATE" "$STATE_NEXT"
    echo "first install failed; no prior archive to restore"
    return 0
  fi
  restore_backup
  rm -f "$STATE" "$STATE_NEXT" "$BACKUP_SUM"
  echo "prior archive restored; cluster state was NOT rolled back; redeploy with offline-run.sh to restore cluster"
}

# apply refuses to run while the state file exists, so without state these temporary
# files are not a restore point.
clear_leftovers() {
  rm -f "$STATE.tmp" "$STATE_NEXT"
}

# A leftover .bak with no state is removed only when it is provably a copy of the active
# archive: same inode, identical, or an interrupted copy (a strict prefix). Anything else
# may be the only copy of the prior archive, so refuse and leave it in place.
check_leftover_backup() {
  local bsize asize
  [ -e "$ACTIVE" ] || return 0
  if [ ! -e "$BACKUP" ] && [ ! -L "$BACKUP" ]; then
    rm -f "$BACKUP_SUM"
    return 0
  fi
  if [ -f "$BACKUP" ] && { [ "$BACKUP" -ef "$ACTIVE" ] || cmp -s "$BACKUP" "$ACTIVE"; }; then
    rm -f "$BACKUP" "$BACKUP_SUM"
    return 0
  fi
  if [ -f "$BACKUP" ]; then
    bsize=$(wc -c < "$BACKUP" | tr -d ' ')
    asize=$(wc -c < "$ACTIVE" | tr -d ' ')
    if [ "$bsize" -lt "$asize" ] && { [ "$bsize" -eq 0 ] || head -c "$bsize" "$ACTIVE" | cmp -s - "$BACKUP"; }; then
      rm -f "$BACKUP" "$BACKUP_SUM"
      return 0
    fi
  fi
  echo "update-package.sh: leftover $BACKUP differs from the active archive and may be the only copy of the prior archive; move it aside and rerun" >&2
  exit 1
}

# Hard check on archives/ for the staged copy. TMPDIR is a warning only, because the
# zarf extraction size is an estimate. On a shared filesystem the extraction must also
# fit beside the staged copy, so the warning threshold is 2.6x instead of 1.6x.
preflight_storage() {
  local package=$1 pkg_kib need_kib free tmp_dir tmp_free factor=16 need_tmp_kib
  pkg_kib=$(( ($(wc -c < "$package" | tr -d ' ') + 1023) / 1024 ))
  need_kib=$(( pkg_kib + 1024 ))
  free=$(free_kb "$ARCHIVES" || true)
  if ! is_number "$free"; then
    echo "update-package.sh: cannot read free space on $ARCHIVES; active archive untouched" >&2
    exit 5
  fi
  if [ "$free" -lt "$need_kib" ]; then
    echo "update-package.sh: insufficient storage on $ARCHIVES: need $need_kib KiB, have $free KiB; active archive untouched" >&2
    exit 5
  fi

  tmp_dir=${TMPDIR:-/tmp}
  if [ "$(fs_dev "$tmp_dir" || true)" = "$(fs_dev "$ARCHIVES" || true)" ]; then
    factor=26
  fi
  need_tmp_kib=$(( pkg_kib * factor / 10 ))
  tmp_free=$(free_kb "$tmp_dir" || true)
  if ! is_number "$tmp_free"; then
    echo "update-package.sh: warning: cannot read free space on $tmp_dir" >&2
  elif [ "$tmp_free" -lt "$need_tmp_kib" ]; then
    echo "update-package.sh: warning: $tmp_dir has $tmp_free KiB free; zarf extraction may need about $need_tmp_kib KiB" >&2
  fi
}

staging_failed() {
  rm -rf "$STAGING"
  echo "update-package.sh: $1; active package untouched" >&2
  exit 2
}

# Writes the backup, its checksum, and the state files. The awaiting-confirm state is
# written first, so the post-deploy step is a single rename; the cutover state is last.
prepare_cutover() {
  local first_install=$1 cutover=phase=cutover done=phase=awaiting-confirm
  if [ "$first_install" -eq 0 ]; then
    { ln "$ACTIVE" "$BACKUP" 2>/dev/null || cp -p "$ACTIVE" "$BACKUP"; } || return 1
    sha256_of "$BACKUP" > "$BACKUP_SUM" || return 1
    grep -Eqx '[0-9a-f]{64}' "$BACKUP_SUM" || return 1
  else
    cutover="$cutover\nfirst_install=1"
    done="$done\nfirst_install=1"
  fi
  printf '%b\n' "$done" > "$STATE_NEXT" || return 1
  write_state "$cutover" || return 1
}

apply() {
  local package=$1 sbom_dir first_install=0

  [ -f "$package" ] || { echo "update-package.sh: package not found: $package" >&2; exit 1; }
  if [ -e "$BACKUP" ] && [ "$package" -ef "$BACKUP" ]; then
    echo "update-package.sh: $package is the backup of the prior archive; copy it elsewhere before applying" >&2
    exit 1
  fi
  acquire_lock

  if [ -f "$STATE" ]; then
    case "$(state_phase)" in
      cutover|awaiting-confirm)
        status
        echo "update-package.sh: prior update not finished; confirm or roll back first" >&2
        exit 1
        ;;
      *)
        echo "update-package.sh: unexpected state: $(tr '\n' ' ' < "$STATE"); inspect it, then run rollback or move $STATE aside" >&2
        exit 1
        ;;
    esac
  fi

  rm -rf "$STAGING"
  clear_leftovers
  check_leftover_backup
  preflight_storage "$package"
  mkdir "$STAGING" || staging_failed "could not create staging directory"
  if ! { cp "$package" "$STAGING/$ACTIVE_NAME" && sync "$STAGING/$ACTIVE_NAME"; }; then
    staging_failed "staging copy failed"
  fi

  sbom_dir=$(mktemp -d "${TMPDIR:-/tmp}/airgap-update-sbom.XXXXXX") || staging_failed "could not create SBOM directory"
  if ! airgap_verify_package "$STAGING/$ACTIVE_NAME" "$sbom_dir"; then
    rm -rf "$STAGING" "$sbom_dir"
    echo "update-package.sh: rejected during staging; active package untouched" >&2
    exit 2
  fi
  rm -rf "$sbom_dir"

  [ -e "$ACTIVE" ] || first_install=1
  if ! prepare_cutover "$first_install"; then
    if [ "$first_install" -eq 0 ]; then
      rm -f "$BACKUP" "$BACKUP_SUM"
    fi
    rm -f "$STATE_NEXT" "$STATE.tmp"
    rm -rf "$STAGING"
    echo "update-package.sh: cutover preparation failed; active package untouched" >&2
    exit 2
  fi

  mv -f "$STAGING/$ACTIVE_NAME" "$ACTIVE"
  rm -rf "$STAGING"

  if ! "${AIRGAP_DEPLOY_CMD:-$SCRIPT_DIR/offline-run.sh}" "$ACTIVE"; then
    echo "update-package.sh: deploy failed; restoring prior archive" >&2
    restore_previous
    exit 3
  fi

  if ! mv -f "$STATE_NEXT" "$STATE"; then
    echo "update-package.sh: deployed, but the state could not be updated; state is still phase=cutover, run rollback" >&2
    exit 1
  fi
  echo "update-package.sh: deployed; run 'update-package.sh confirm' or 'update-package.sh rollback'"
}

rollback() {
  acquire_lock
  require_state

  if state_is_first_install; then
    rm -f "$ACTIVE" "$STATE" "$STATE_NEXT"
    echo "first install rolled back; no prior archive"
    exit 0
  fi
  restore_backup
  # State goes before the checksum: the checksum is the evidence a rerun needs.
  rm -f "$STATE" "$STATE_NEXT"
  rm -f "$BACKUP_SUM"
  echo "rollback complete; cluster state was NOT rolled back; redeploy with offline-run.sh to restore cluster"
}

confirm() {
  acquire_lock
  require_state

  case "$(state_phase)" in
    cutover)
      echo "update-package.sh: deploy may not have completed; run rollback" >&2
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
