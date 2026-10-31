#!/usr/bin/env bash
# Restore a previously backed-up C.O.R.E. SQLite DB.
# Usage: ./scripts/restore_core.sh <backup_file>
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <backup_file>"
  exit 1
fi
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DB_DIR="$ROOT/data"
DB_FILE="$DB_DIR/core.sqlite3"
BACKUP="$1"
if [[ ! -f "$BACKUP" ]]; then
  echo "Backup file not found: $BACKUP"
  exit 1
fi
cp "$BACKUP" "$DB_FILE"
echo "Database restored from $BACKUP"

