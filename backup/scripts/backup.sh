#!/bin/sh
set -eu

BACKUP_DIR="/backup"
DATE="$(date +%F_%H%M%S)"

log() { echo "[INFO] $*"; }
die() {
	echo "[ERROR] $*" >&2
	exit 1
}

log "Backup started at $DATE"

# ---------- PostgreSQL ----------
PG_BACKUP_DIR="$BACKUP_DIR/postgres"
mkdir -p "$PG_BACKUP_DIR"
echo "[INFO] Backing up PostgreSQL (nautilus_trader)..."

export PGPASSWORD="$POSTGRES_PASSWORD"

OUT="$PG_BACKUP_DIR/nautilus_trader_${DATE}.dump.gz"
ERR="$PG_BACKUP_DIR/pg_dump_${DATE}.err"
TMP_DUMP="$PG_BACKUP_DIR/nautilus_trader_${DATE}.dump"
TMP_GZ="${OUT}.tmp"

pg_dump -h postgres -U "$POSTGRES_USER" -d nautilus_trader -Fc >"$TMP_DUMP" 2>"$ERR" || {
  echo "[ERROR] pg_dump failed"; tail -n 80 "$ERR" >&2; exit 1;
}
[ -s "$TMP_DUMP" ] || { echo "[ERROR] pg_dump produced empty output"; tail -n 80 "$ERR" >&2; exit 1; }

gzip -c "$TMP_DUMP" >"$TMP_GZ" || { echo "[ERROR] gzip failed"; exit 1; }
rm -f "$TMP_DUMP"
mv -f "$TMP_GZ" "$OUT"
echo "[INFO] PostgreSQL backup written: $OUT"

# ---------- Redis ----------
REDIS_BACKUP_DIR="$BACKUP_DIR/redis"
mkdir -p "$REDIS_BACKUP_DIR"
log "Backing up Redis..."

if [ -f "/data/dump.rdb" ]; then
	cp -f /data/dump.rdb "$REDIS_BACKUP_DIR/dump_${DATE}.rdb"
	[ -s "$REDIS_BACKUP_DIR/dump_${DATE}.rdb" ] || die "Redis dump copied but empty"
else
	log "No /data/dump.rdb found; skipping Redis RDB copy"
fi

# ---------- Prometheus ----------
PROM_BACKUP_DIR="$BACKUP_DIR/prometheus"
mkdir -p "$PROM_BACKUP_DIR"
log "Backing up Prometheus..."
tar czf "$PROM_BACKUP_DIR/prometheus_${DATE}.tar.gz" -C /prometheus . || die "Prometheus tar failed"

# ---------- Grafana ----------
GRAFANA_BACKUP_DIR="$BACKUP_DIR/grafana"
mkdir -p "$GRAFANA_BACKUP_DIR"
log "Backing up Grafana..."
tar czf "$GRAFANA_BACKUP_DIR/grafana_${DATE}.tar.gz" -C /var/lib/grafana . || die "Grafana tar failed"

# ---------- Alertmanager ----------
ALERT_BACKUP_DIR="$BACKUP_DIR/alertmanager"
mkdir -p "$ALERT_BACKUP_DIR"
log "Backing up Alertmanager..."
tar czf "$ALERT_BACKUP_DIR/alertmanager_${DATE}.tar.gz" -C /alertmanager . || die "Alertmanager tar failed"

# ---------- Cleanup: keep last 30 days ----------
log "Cleaning backups older than 30 days..."
find "$BACKUP_DIR" -type f -mtime +30 \( -name "*.gz" -o -name "*.rdb" -o -name "*.tar.gz" \) -delete

log "Backup completed successfully!"

