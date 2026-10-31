# scripts/backup_alexandria.sh
#!/usr/bin/env bash
# Backup the Alexandria SQLite DB to a timestamped file.
# Usage: ./scripts/backup_alexandria.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DB_DIR="$ROOT/alexandria_project/data"
DB_FILE="$DB_DIR/alexandria.sqlite3"
if [[ ! -f "$DB_FILE" ]]; then
  echo "Database not found at $DB_FILE"
  exit 1
fi
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP="${DB_FILE}.${TIMESTAMP}.bak"
cp "$DB_FILE" "$BACKUP"
echo "Backup created at $BACKUP"
