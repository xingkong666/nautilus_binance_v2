<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# docs/

## Purpose
Human-readable documentation for the nautilus_binance_v2 trading system. Covers system architecture, operational runbooks, risk management philosophy, monitoring procedures, go-live checklists, and strategy research reports. These files are reference material for engineers, operators, and AI agents working on the system — they do **not** contain executable code or configuration.

## Key Files

| File | Description |
|------|-------------|
| `architecture.md` | System architecture: design principles, component diagram, EventBus data flow, module dependency rules, and the signal→fill pipeline |
| `runbook.md` | Operational runbook: start/stop procedures, emergency shutdown, common failure modes, and recovery steps |
| `monitoring.md` | Monitoring operations guide: Prometheus metrics reference, Grafana dashboard walkthrough, AlertManager rules, and alert-response procedures |
| `risk.md` | Risk management explanation: three-layer risk architecture (PreTrade / RealTime / PostTrade), CircuitBreaker states, DrawdownControl logic, and tuning guidelines |
| `go_live_checklist.md` | Pre-production go-live checklist: credential verification, config validation, canary dry-run steps, and sign-off gates |
| `strategy_200pct_blueprint_2026_03.md` | Research blueprint targeting 200% annual return: strategy selection rationale, parameter constraints, and expected performance bounds (March 2026) |
| `turtle_backtest_4h_sensitivity.md` | Sensitivity analysis of the Turtle strategy on 4-hour bars: parameter sweep results, regime dependency, and robustness conclusions |
| `vegas_tunnel_rollout_2026_03.md` | Vegas Tunnel strategy rollout plan (March 2026): phased deployment schedule, risk gates, and monitoring KPIs |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| *(none)* | All documentation lives as flat Markdown files in this directory |

## For AI Agents

### Working In This Directory
- **Read before editing**: these docs describe intent and constraints that govern the rest of the codebase. Understand them before making architectural changes elsewhere.
- **Keep docs in sync with code**: after modifying `src/`, update the relevant doc here (especially `architecture.md` and `runbook.md`).
- **Language**: all docs are written in Chinese (Simplified). Maintain that language when editing.
- **No executable content**: do not add scripts, YAML configs, or Python code snippets that diverge from `configs/` or `src/`. Pseudocode and illustrative snippets are fine.
- **Strategy reports** (`strategy_200pct_blueprint_2026_03.md`, `turtle_backtest_4h_sensitivity.md`, `vegas_tunnel_rollout_2026_03.md`) are historical research artifacts — treat them as read-only unless explicitly revising a research document.

### Common Patterns
- **Checking current architecture**: read `architecture.md` first, then cross-reference `src/AGENTS.md` for code-level detail.
- **Debugging a production incident**: `runbook.md` → `monitoring.md` for the response playbook.
- **Evaluating risk parameter changes**: `risk.md` describes the three-layer chain and what each parameter controls.
- **Preparing a production deployment**: follow `go_live_checklist.md` step-by-step; do not skip canary dry-run.

## Dependencies

### Internal
- `../CLAUDE.md` — project conventions that this documentation describes
- `../src/` — the implementation these docs document; changes there should trigger doc updates here
- `../configs/` — config schemas referenced in `risk.md`, `monitoring.md`, and `runbook.md`

### External
- [NautilusTrader docs](https://nautilustrader.io/docs/) — upstream framework documentation referenced by `architecture.md`
- Binance API docs — referenced by deployment and monitoring procedures

<!-- MANUAL: -->
