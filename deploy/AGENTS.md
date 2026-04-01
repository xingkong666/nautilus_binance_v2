<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# deploy/

## Purpose
Production deployment artifacts for running nautilus_binance_v2 as a managed system service on Linux. Currently contains the systemd unit file that supervises the live trading process, handles automatic restarts, and routes stdout/stderr to persistent log files under `data/`.

## Key Files

| File | Description |
|------|-------------|
| `systemd/nautilus-live.service` | systemd unit file for the production live-trading process: runs as `root`, loads `.env` credentials via `EnvironmentFile`, sets `CONFIRM_LIVE=YES`, restarts on failure with 10 s backoff, and appends logs to `data/live.stdout.log` / `data/live.stderr.log` |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `systemd/` | systemd unit files for Linux service management |

## For AI Agents

### Working In This Directory
- **Minimal surface**: this directory intentionally contains only the systemd unit. Do not add Docker application images, Kubernetes manifests, or CI pipeline files here without explicit instruction — the project runs the application directly on the host (see `../docker-compose.yml` for infrastructure-only containers).
- **`EnvironmentFile` path is hardcoded** to `/root/workSpace/nautilus_binance_v2/.env`. If the workspace path changes, update `WorkingDirectory` and `EnvironmentFile` in the unit file together.
- **`CONFIRM_LIVE=YES` is set in the unit** — this is intentional; the guard exists to prevent accidental execution outside systemd. Do not remove it.
- **`TimeoutStopSec=45`** gives the process 45 seconds to flush state and close positions gracefully on `systemctl stop`. Do not reduce this value without verifying that `container.teardown()` + `adapter.stop()` complete within the new timeout.
- **Log paths** (`data/live.stdout.log`, `data/live.stderr.log`) are relative to `WorkingDirectory`. Ensure the `data/` directory exists before enabling the service.
- **After editing the unit file**, reload the daemon: `systemctl daemon-reload && systemctl restart nautilus-live`.

### Common Patterns
- **Install the service**:
  ```bash
  cp deploy/systemd/nautilus-live.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable nautilus-live
  systemctl start nautilus-live
  ```
- **Check service status**:
  ```bash
  systemctl status nautilus-live
  journalctl -u nautilus-live -f
  ```
- **Graceful stop** (allows position cleanup):
  ```bash
  systemctl stop nautilus-live   # waits up to TimeoutStopSec=45s
  ```
- **Emergency kill** (skips cleanup — use only in crisis):
  ```bash
  systemctl kill --kill-who=main --signal=SIGKILL nautilus-live
  ```

## Dependencies

### Internal
- `../scripts/run_live_prod.sh` — the `ExecStart` target; must be executable and reference the correct `uv` / Python path
- `../.env` — loaded by `EnvironmentFile`; must contain `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `POSTGRES_*` vars
- `../data/` — log output directory (`live.stdout.log`, `live.stderr.log` written here)
- `../docs/runbook.md` — operational procedures including start/stop and recovery steps
- `../docs/go_live_checklist.md` — must be completed before enabling this service in production

### External
- systemd ≥ 240 (standard on Ubuntu 20.04+)
- `docker.service` — listed as `After=` dependency; infrastructure containers (Postgres, Redis) must be running before the trading process starts

<!-- MANUAL: -->
