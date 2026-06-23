# Strategy Registry — Design Spec (M5.1)

**Status:** Spec / Implementation in progress  
**Branch:** `claude/strategy-registry-spec-7elyui`  
**Milestone:** M5.1 (Week 28)

---

## 1. Purpose

The Strategy Registry is a DB-backed catalog that tracks every strategy the system has ever known about. It is the authoritative source of truth for:

- Which strategies are currently active for paper or live trading
- What configuration file each strategy was registered with (and its SHA-256 fingerprint)
- How a strategy has performed across backtest, paper, and live periods

**Key invariant from CLAUDE.md:** Activation means passing `--strategy-config` for a registered strategy; deactivation is a status update, not a code deletion.

---

## 2. Status Lifecycle

```
              register
                 │
                 ▼
          ┌─ backtesting ─┐
          │               │
        promote         archive
          │               │
          ▼               ▼
        paper ──────► archived
          │               ▲
        promote           │
          │             archive
          ▼               │
         live ────────────┘
          │
        step-down
          │
          ▼
        paper
```

| Transition | Allowed |
|-----------|---------|
| `backtesting` → `paper` | Yes |
| `backtesting` → `archived` | Yes |
| `paper` → `live` | Yes (requires C8 clearance check) |
| `paper` → `backtesting` | Yes (step-down) |
| `paper` → `archived` | Yes |
| `live` → `paper` | Yes (step-down) |
| `live` → `archived` | Yes |
| `archived` → anything | **No** — terminal state |

Only one strategy per `(strategy_id)` may be in `paper` or `live` status at a time; the registry enforces this at the DB level via a partial unique index.

---

## 3. Database Schema

### 3.1 `strategies` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL PRIMARY KEY` | Internal surrogate key |
| `strategy_id` | `TEXT NOT NULL UNIQUE` | Human-readable ID; matches YAML file stem convention `v{N}_{short_description}` |
| `config_path` | `TEXT NOT NULL` | Repo-relative path, e.g. `config/strategy/v1_base_momentum.yaml` |
| `config_sha256` | `TEXT NOT NULL` | SHA-256 of config file content at registration time; guards C6 (immutability) |
| `status` | `TEXT NOT NULL` | Enum: `backtesting` \| `paper` \| `live` \| `archived` |
| `version` | `INTEGER NOT NULL` | `version` field from YAML |
| `name` | `TEXT NOT NULL` | `name` field from YAML (display name) |
| `description` | `TEXT` | `description` field from YAML |
| `portfolio_method` | `TEXT` | `portfolio.method` from YAML for quick filtering |
| `n_long` | `INTEGER` | `portfolio.n_long` |
| `rebalance_frequency` | `TEXT` | `portfolio.rebalance_frequency` |
| `registered_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `activated_paper_at` | `TIMESTAMPTZ` | Timestamp of first `paper` status transition |
| `activated_live_at` | `TIMESTAMPTZ` | Timestamp of first `live` status transition |
| `archived_at` | `TIMESTAMPTZ` | Timestamp of `archived` status transition |
| `notes` | `TEXT` | Free-text operator notes |

**Indexes:**
- `UNIQUE (strategy_id)` — enforced by column constraint
- `ix_strategies_status` on `(status)` — for fast active-strategy lookups
- Partial unique index `uix_strategies_one_paper` on `(status)` WHERE `status = 'paper'` — prevents two strategies in paper simultaneously *(see note below)*
- Partial unique index `uix_strategies_one_live` on `(status)` WHERE `status = 'live'`

> **Note on one-active-per-status constraint:** The partial unique indexes prevent a second row from entering `paper` or `live` while one is already there. This is intentional for Phase 5: the system runs one strategy per mode at a time. If multi-strategy paper/live support is needed in a future phase, these constraints will be replaced by an explicit `is_active` flag per slot.

### 3.2 `strategy_status_history` table

Append-only audit trail of every status transition. Never updated or deleted (C3).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL PRIMARY KEY` | |
| `strategy_id` | `TEXT NOT NULL REFERENCES strategies(strategy_id)` | |
| `from_status` | `TEXT` | NULL for initial registration |
| `to_status` | `TEXT NOT NULL` | |
| `transitioned_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `operator_notes` | `TEXT` | Reason for transition (required for `live` transitions) |

**Index:** `ix_strategy_status_history_strategy_id` on `(strategy_id, transitioned_at DESC)`

### 3.3 `strategy_performance_snapshots` table

Stores point-in-time performance snapshots for backtest, paper, and live periods. Append-only; never overwrite (C3).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL PRIMARY KEY` | |
| `strategy_id` | `TEXT NOT NULL REFERENCES strategies(strategy_id)` | |
| `snapshot_date` | `DATE NOT NULL` | Date the snapshot was computed |
| `period_type` | `TEXT NOT NULL` | `backtest` \| `paper` \| `live` |
| `period_start` | `DATE` | Start of the performance period |
| `period_end` | `DATE` | End of the performance period |
| `annualized_return` | `NUMERIC(18, 6)` | Decimal form (0.12 = 12%) |
| `annualized_volatility` | `NUMERIC(18, 6)` | Decimal, annualized |
| `sharpe_ratio` | `NUMERIC(18, 6)` | |
| `max_drawdown` | `NUMERIC(18, 6)` | Decimal, negative (−0.08 = −8%) |
| `information_ratio` | `NUMERIC(18, 6)` | vs. benchmark; NULL if not yet computable |
| `total_trades` | `INTEGER` | |
| `data_version` | `TEXT` | MLflow data manifest path (required for `backtest`; C7) |
| `mlflow_run_id` | `TEXT` | MLflow run ID for backtest snapshots |
| `recorded_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

**Index:** `ix_strategy_perf_strategy_period` on `(strategy_id, period_type, snapshot_date DESC)`

---

## 4. Python Interface

### 4.1 Module layout

```
strategy_registry/
├── __init__.py          # re-exports StrategyRegistry, StrategyStatus
├── models.py            # SQLAlchemy ORM: Strategy, StrategyStatusHistory, StrategyPerformanceSnapshot
├── registry.py          # StrategyRegistry class — all business logic
├── loader.py            # YAML loader + SHA-256 fingerprinting
└── cli.py               # CLI: python -m strategy_registry <subcommand>
```

### 4.2 `StrategyStatus` enum

```python
class StrategyStatus(str, enum.Enum):
    BACKTESTING = "backtesting"
    PAPER       = "paper"
    LIVE        = "live"
    ARCHIVED    = "archived"
```

### 4.3 `StrategyRegistry` API

```python
class StrategyRegistry:
    def __init__(self, db_url: str) -> None: ...

    def register(
        self,
        strategy_id: str,
        config_path: str,
        notes: str | None = None,
    ) -> Strategy:
        """
        Parse YAML at config_path, compute SHA-256, insert Strategy row with
        status=backtesting. Raises StrategyAlreadyRegisteredError if strategy_id already
        exists (strategy_id is permanent — create v2_… instead of re-registering).
        """

    def transition(
        self,
        strategy_id: str,
        to_status: StrategyStatus,
        operator_notes: str | None = None,
    ) -> Strategy:
        """
        Validate the transition is allowed, enforce one-active-per-mode constraints,
        require operator_notes for live transitions (C8 path), update the strategy
        row, and append a StrategyStatusHistory record.
        """

    def get(self, strategy_id: str) -> Strategy: ...

    def list(
        self,
        status: StrategyStatus | None = None,
    ) -> list[Strategy]: ...

    def record_performance(
        self,
        strategy_id: str,
        snapshot: PerformanceSnapshot,
    ) -> StrategyPerformanceSnapshot:
        """Append a performance snapshot; validates data_version for backtest (C7)."""

    def verify_config_integrity(self, strategy_id: str) -> bool:
        """
        Re-hash the config file at the registered config_path and compare against
        stored config_sha256. Returns True if unchanged. Raises ConfigDriftError
        if the hash differs (C6 guard).
        """
```

### 4.4 CLI subcommands

```
python -m strategy_registry register   --strategy-id v1_base_momentum \
                                       --config-path config/strategy/v1_base_momentum.yaml \
                                       [--notes "Initial registration"]

python -m strategy_registry status     --strategy-id v1_base_momentum
                                       --to paper
                                       [--notes "Moving to paper trading"]

python -m strategy_registry list       [--status paper]

python -m strategy_registry show       --strategy-id v1_base_momentum

python -m strategy_registry verify     --strategy-id v1_base_momentum
                                       # Re-hashes config and confirms no drift (C6)

python -m strategy_registry perf       --strategy-id v1_base_momentum
                                       --period-type backtest
                                       --period-start 2022-07-11
                                       --period-end 2024-12-31
                                       --sharpe 0.82
                                       --annualized-return 0.14
                                       --max-drawdown -0.09
                                       --data-version rqis-snapshots/manifests/2026-06-14/manifest.json
                                       --mlflow-run-id <run_id>
```

---

## 5. Integrity and Safety Rules

| Rule | Mechanism |
|------|-----------|
| C6 — config immutability | `verify_config_integrity()` called by the paper/live pipeline before every run; `register()` stores SHA-256 at registration |
| C7 — data version in backtest | `record_performance()` requires non-empty `data_version` when `period_type == "backtest"` |
| C3 — no deletes on audit tables | `strategy_status_history` and `strategy_performance_snapshots` have no `DELETE` path |
| One paper/live at a time | DB partial unique indexes; Python layer raises `ConflictingActiveStrategyError` with a helpful message |
| `archived` is terminal | `transition()` raises `InvalidTransitionError` if `from_status == archived` |
| Live requires notes | `transition(to_status=live)` raises `MissingOperatorNotesError` if `operator_notes` is blank |

---

## 6. Integration Points

| System | How Registry Integrates |
|--------|------------------------|
| Paper pipeline scripts (Steps 1–8) | Step 1 (`paper_readiness_check`) will call `registry.verify_config_integrity()` to catch silent config drift before a run |
| Airflow DAG (M5.4) | DAG start task queries `registry.list(status=paper)` to find the active strategy config |
| Tearsheets (M5.3) | Pulls `strategy_performance_snapshots` to render multi-period performance tables |
| Streamlit dashboard (M5.8) | Displays registry state; operator triggers status transitions from the UI |
| MLflow | `mlflow_run_id` stored in performance snapshots links metrics back to the experiment tracking server |

---

## 7. Out of Scope (Phase 5.1)

- Multi-strategy paper/live mode (single active strategy per mode enforced for now)
- Strategy scheduling / rotation automation (M5.4 Airflow DAG)
- Strategy comparison UI (M5.8 dashboard)
- Live status transitions (C8 requires 4-week paper qualification — not unlocked yet)
