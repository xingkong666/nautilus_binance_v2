#!/bin/sh
set -e

BACKUP_DIR="/backup"
DATE=$(date +%F_%H%M%S)

echo "[INFO] Backup started at $DATE"

# ---------- PostgreSQL ----------
PG_BACKUP_DIR="$BACKUP_DIR/postgres"
mkdir -p "$PG_BACKUP_DIR"
echo "[INFO] Backing up PostgreSQL..."
export PGPASSWORD="${POSTGRES_PASSWORD}"
pg_dumpall -U "${POSTGRES_USER}" | gzip > "$PG_BACKUP_DIR/pg_backup_${DATE}.sql.gz"

# ---------- Redis ----------
REDIS_BACKUP_DIR="$BACKUP_DIR/redis"
mkdir -p "$REDIS_BACKUP_DIR"
echo "[INFO] Backing up Redis..."
if [ -f "/data/dump.rdb" ]; then
  cp /data/dump.rdb "$REDIS_BACKUP_DIR/dump_${DATE}.rdb"
fi

# ---------- Prometheus ----------
PROM_BACKUP_DIR="$BACKUP_DIR/prometheus"
mkdir -p "$PROM_BACKUP_DIR"
echo "[INFO] Backing up Prometheus..."
tar czf "$PROM_BACKUP_DIR/prometheus_${DATE}.tar.gz" -C /prometheus .

# ---------- Grafana ----------
GRAFANA_BACKUP_DIR="$BACKUP_DIR/grafana"
mkdir -p "$GRAFANA_BACKUP_DIR"
echo "[INFO] Backing up Grafana..."
tar czf "$GRAFANA_BACKUP_DIR/grafana_${DATE}.tar.gz" -C /var/lib/grafana .

# ---------- Alertmanager ----------
ALERT_BACKUP_DIR="$BACKUP_DIR/alertmanager"
mkdir -p "$ALERT_BACKUP_DIR"
echo "[INFO] Backing up Alertmanager..."
tar czf "$ALERT_BACKUP_DIR/alertmanager_${DATE}.tar.gz" -C /alertmanager .

# ---------- Cleanup: keep last 30 days ----------
echo "[INFO] Cleaning backups older than 30 days..."
find "$BACKUP_DIR" -type f -mtime +30 -name "*.gz" -delete
find "$BACKUP_DIR" -type f -mtime +30 -name "*.rdb" -delete

echo "[INFO] Backup completed successfully!"
