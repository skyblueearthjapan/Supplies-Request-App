#!/bin/sh
# 毎日 3:00（日本時間）に pg_dump を取り、BACKUP_KEEP_DAYS 日より古いものを消す常駐コンテナ。
#
# 社内の前例（Project Management）では、busybox crond がジョブに環境変数を渡さず、
# ダンプが 0 バイトのまま何か月も気づかれなかった。そのため:
# - 起動時に値を焼き込んだ .pgpass と実行スクリプトを作り、cron からはそれを呼ぶ
# - ダンプの大きさを確かめ、小さすぎたら FAILED を出して壊れたファイルを残さない
# - 起動直後に 1 回取って、設定の誤りにすぐ気づけるようにする

set -eu

PGPASS=/root/.pgpass
umask 077
printf 'db:5432:%s:%s:%s\n' "$POSTGRES_DB" "$POSTGRES_USER" "$POSTGRES_PASSWORD" > "$PGPASS"
umask 022

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
RUNNER=/usr/local/bin/run_backup.sh
cat > "$RUNNER" <<EOF
#!/bin/sh
export PGPASSFILE=$PGPASS
export TZ=Asia/Tokyo
out=/backups/chozouhin_\$(date +%Y%m%d_%H%M).dump
tmp="\$out.partial"
if /usr/local/bin/pg_dump -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "\$tmp"; then
  size=\$(wc -c < "\$tmp")
  if [ "\$size" -lt 1024 ]; then
    echo "[backup] FAILED \$out too small (\$size bytes)"
    rm -f "\$tmp"
    exit 1
  fi
  mv "\$tmp" "\$out"
  echo "[backup] ok \$out \$size bytes"
  find /backups -maxdepth 1 -name 'chozouhin_*.dump' -mtime +$KEEP_DAYS -print -delete
else
  echo "[backup] FAILED \$out"
  rm -f "\$tmp"
  exit 1
fi
EOF
chmod 755 "$RUNNER"

CRON_FILE=/etc/crontabs/root
mkdir -p /etc/crontabs
echo "0 3 * * * $RUNNER" > "$CRON_FILE"
echo "[backup] crontab written:"
cat "$CRON_FILE"

"$RUNNER" || true

exec crond -f -l 8
