#!/usr/bin/env bash
# 每日备份包装（systemd timer 调用；阶段 8）
# 用法：APP_DIR=/opt/portal OUT_ROOT=/opt/portal-backups KEEP=14 scripts/portal_backup_daily.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/portal}"
OUT_ROOT="${OUT_ROOT:-/opt/portal-backups}"
KEEP="${KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_ROOT/daily-$STAMP"

mkdir -p "$OUT_ROOT"
# --keep 语义：根目录下"含 manifest.json 的备份目录"总共保留 KEEP 份（含本轮）
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/portal_backup.py" \
  --data-dir "$APP_DIR" --out "$OUT" --keep "$KEEP" --tar --label "daily $STAMP"
