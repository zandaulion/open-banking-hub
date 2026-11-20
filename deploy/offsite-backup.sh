#!/usr/bin/bash
# Encrypted off-site copy of the latest local Bank Hub snapshot, with restic over SFTP.
# Settings come from an env file outside the repository (see deploy/bankhub-offsite-backup.service).
#   backup        upload the newest snapshot
#   maintain      thin out old snapshots and verify part of the repository
#   restore-test  restore the latest snapshot to a temporary directory and check the databases
set -euo pipefail
umask 077

: "${BANKHUB_DATA_DIR:?}" "${RESTIC_REPOSITORY:?}" "${RESTIC_PASSWORD_FILE:?}" "${RESTIC_SSH_CONFIG:?}" "${RESTIC_HOST:?}"
ssh_host="${RESTIC_REPOSITORY#sftp:}"; ssh_host="${ssh_host%%:*}"
restic=(restic --repo "$RESTIC_REPOSITORY" --password-file "$RESTIC_PASSWORD_FILE"
        -o "sftp.command=ssh -F $RESTIC_SSH_CONFIG $ssh_host -s sftp")

latest_snapshot() {
  local name
  name="$(find "$BANKHUB_DATA_DIR/backups" -mindepth 1 -maxdepth 1 -type d -name '2*Z' -printf '%f\n' | sort | tail -1)"
  [[ "$name" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo "No local snapshot found." >&2; return 1; }
  printf '%s/%s\n' "$BANKHUB_DATA_DIR/backups" "$name"
}

case "${1:-}" in
  init)
    "${restic[@]}" cat config >/dev/null 2>&1 || "${restic[@]}" init
    ;;
  backup)
    snap="$(latest_snapshot)"
    [[ -f "$snap/bankhub.db" ]] || { echo "Latest snapshot has no bankhub.db." >&2; exit 1; }
    # Upload from inside the snapshot so restored paths do not depend on the timestamp.
    cd "$snap" && "${restic[@]}" backup . --host "$RESTIC_HOST" --tag bankhub --skip-if-unchanged
    ;;
  maintain)
    "${restic[@]}" forget --host "$RESTIC_HOST" --tag bankhub --keep-daily 14 --keep-weekly 8 --keep-monthly 24 --prune
    "${restic[@]}" check --read-data-subset=10%
    ;;
  restore-test)
    dir="$(mktemp -d /tmp/bankhub-restore.XXXXXX)"
    trap '[[ "$dir" == /tmp/bankhub-restore.* ]] && rm -rf -- "$dir"' EXIT
    "${restic[@]}" restore latest --host "$RESTIC_HOST" --tag bankhub --target "$dir"
    for db in bankhub.db web.db; do
      f="$(find "$dir" -name "$db" | head -1)"
      [[ -n "$f" ]] || { echo "Restore is missing $db" >&2; exit 1; }
      python3 -c "import sqlite3,sys; r=sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0]; print(sys.argv[2], r); sys.exit(r!='ok')" "$f" "$db"
    done
    ;;
  *) echo "Usage: $0 {init|backup|maintain|restore-test}" >&2; exit 2 ;;
esac
