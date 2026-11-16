#!/usr/bin/env bash
# 業務データ(DB + 領収書画像)のバックアップ。GitHubには置かず、別ディスク/別サーバー/クラウドストレージへ。
#   使い方: DATA_DIR=/var/hakoship/data BACKUP_DIR=/mnt/backup/hakoship ./scripts/backup.sh
#   cron例: 0 2 * * * DATA_DIR=... BACKUP_DIR=... /path/to/scripts/backup.sh
set -euo pipefail
DATA_DIR="${DATA_DIR:-./data}"
BACKUP_DIR="${BACKUP_DIR:?BACKUP_DIR を指定してください}"
KEEP_DAYS="${KEEP_DAYS:-90}"
stamp=$(date +%Y%m%d-%H%M%S)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$BACKUP_DIR"
python3 - "$DATA_DIR/app.db" "$work/app.db" <<'PY'   # 稼働中でも整合性のあるコピーを取る
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
[ -d "$DATA_DIR/receipts" ] && cp -a "$DATA_DIR/receipts" "$work/receipts"
tar -czf "$BACKUP_DIR/mbs-expense-$stamp.tar.gz" -C "$work" .
chmod 600 "$BACKUP_DIR/mbs-expense-$stamp.tar.gz"
find "$BACKUP_DIR" -name 'mbs-expense-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $BACKUP_DIR/mbs-expense-$stamp.tar.gz"
