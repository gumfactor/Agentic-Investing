# Strategy Registry — Design Spec (M5.1)

**Status:** Schema redesign (v2)
**Branch:** `claude/strategy-registry-spec-7elyui`
**Milestone:** M5.1 (Week 28)

---

## 1. Purpose

The Strategy Registry is the authoritative catalog for everything the system knows about a strategy — what it is (config, fingerprint, experiment history) and where it is in its operational life (backtesting → paper → live → archived). One module, one first-class entity.

**Key invariant from CLAUDE.md:** Activation means passing `--strategy-config` for a registered strategy; deactivation is a status update, not a code deletion.

---

## 2. Two-level identity model

A strategy exists at two levels:

**Definition level** — what the strategy *is*. A strategy can have multiple config versions over its research lifetime (parameter sweeps, pre-registration iteration). Each unique canonical config gets a row in `strategy_definitions` keyed by `(strategy_id, config_hash)`. Experiment runs (backtests, signal IC tests, paper dry-runs) attach to a specific `(strategy_id, config_hash)` pair and can be recorded before the strategy is formally registered.

**Lifecycle level** — where the strategy *is* operationally. When a strategy is formally registered for deployment, it gets a single row in `strategies` that pins one canonical config hash and carries the status state machine. This is the C6 boundary: once a strategy enters `paper` or `live`, its canonical config hash is frozen.

```
strategy_definitions  ← research artifacts, config history, run records
        │
        │ canonical_config_hash FK (set at register time)
        ▼
    strategies        ← operational lifecycle, one row per strategy_id
        │
        ▼
strategy_status_history  ← append-only transition audit (C3)
```

`strategy_runs` links to `strategy_definitions` (not `strategies`) — runs can exist for pre-registration configs.

---

## 3. Status Lifecycle

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
| `paper` → `live` | Yes (requires C8 clearance; `operator_notes` mandatory) |
| `paper` → `backtesting` | Yes (step-down) |
| `paper` → `archived` | Yes |
| `live` → `paper` | Yes (step-down) |
| `live` → `archived` | Yes |
| `archived` → anything | **No** — terminal |

Only one strategy at a time may hold `paper` status; only one may hold `live` status. Enforced at the DB level via partial unique indexes.

---

## 4. Database Schema

Four tables. No `strategy_performance_snapshots` — metrics live on `strategy_runs` as JSONB, keeping the schema flexible and avoiding a second place to query for the same data.

---

### 4.1 `strategy_definitions`

The research layer. One row per unique `(strategy_id, config_hash)`. Multiple rows per `strategy_id` are expected during iterative pre-registration development.

| Column | Type | Notes |
|--------|------|-------|
| `strategy_id` | `TEXT NOT NULL` | Format enforced: `^[a-z][a-z0-9_]{2,99}$` |
| `config_hash` | `TEXT NOT NULL` | 64-char hex SHA-256 of the **canonical** config (runtime keys excluded — see §6) |
| `name` | `TEXT NOT NULL` | `name` field from YAML |
| `version` | `INTEGER NOT NULL` | `version` field from YAML; must be > 0 |
| `description` | `TEXT` | |
| `portfolio_method` | `TEXT` | `portfolio.method` from YAML |
| `n_long` | `INTEGER` | `portfolio.n_long` |
| `rebalance_frequency` | `TEXT` | `portfolio.rebalance_frequency` |
| `config` | `JSONB NOT NULL` | Full canonical config (runtime keys stripped, keys sorted) stored for auditability |
| `source_path` | `TEXT` | Repo-relative path to the YAML at fingerprint time |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

**Primary key:** `(strategy_id, config_hash)`

**Constraints:**
- `ck_strategy_definitions_strategy_id`: `strategy_id ~ '^[a-z][a-z0-9_]{2,99}$'`
- `ck_strategy_definitions_version_positive`: `version > 0`
- `ck_strategy_definitions_hash_length`: `length(config_hash) = 64`
- `uq_strategy_definitions_version`: `UNIQUE (strategy_id, version)` — a given version number maps to exactly one canonical config per strategy

**Indexes:**
- `ix_strategy_definitions_strategy_id` on `(strategy_id)` — fetch all configs for a strategy
- `ix_strategy_definitions_created` on `(created_at DESC)`

---

### 4.2 `strategies`

The operational/lifecycle layer. One row per `strategy_id`, created at formal registration. Pins one canonical config hash.

| Column | Type | Notes |
|--------|------|-------|
| `strategy_id` | `TEXT NOT NULL` | PK; same format constraint as definitions |
| `canonical_config_hash` | `TEXT NOT NULL` | FK → `strategy_definitions(strategy_id, config_hash)`; the config this strategy is officially running against |
| `status` | `TEXT NOT NULL` | `backtesting` \| `paper` \| `live` \| `archived` |
| `strategy_family` | `TEXT` | Grouping label shared across related versions, e.g. `"base_momentum"`. Nullable for one-off strategies. |
| `supersedes_strategy_id` | `TEXT` | FK → `strategies(strategy_id)`; explicit predecessor link. Validated at registration. |
| `registered_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `activated_paper_at` | `TIMESTAMPTZ` | Timestamp of first `paper` transition |
| `activated_live_at` | `TIMESTAMPTZ` | Timestamp of first `live` transition |
| `archived_at` | `TIMESTAMPTZ` | Timestamp of `archived` transition |
| `notes` | `TEXT` | Free-text operator notes set at registration |

**Primary key:** `strategy_id`

**Foreign keys:**
- `fk_strategies_definition`: `(strategy_id, canonical_config_hash)` → `strategy_definitions(strategy_id, config_hash)`
- `fk_strategies_supersedes`: `supersedes_strategy_id` → `strategies(strategy_id)` ON DELETE RESTRICT

**Constraints:**
- `ck_strategies_status`: `status IN ('backtesting', 'paper', 'live', 'archived')`
- `ck_strategies_strategy_id`: `strategy_id ~ '^[a-z][a-z0-9_]{2,99}$'`

**Indexes:**
- `ix_strategies_status` on `(status)`
- `ix_strategies_family` on `(strategy_family)`
- `uix_strategies_one_paper`: `UNIQUE (status) WHERE status = 'paper'` — at most one strategy in paper
- `uix_strategies_one_live`: `UNIQUE (status) WHERE status = 'live'` — at most one strategy in live

> **Why no surrogate `id`?** `strategy_id` is already a stable, human-meaningful, unique key. A surrogate would just add joins without benefit.

---

### 4.3 `strategy_runs`

Append-only record of every experiment run against a specific `(strategy_id, config_hash)`. Can be written before formal registration. Never updated or deleted after `status` reaches a terminal value (`passed`, `failed`, `blocked`) — C3.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `strategy_id` | `TEXT NOT NULL` | |
| `config_hash` | `TEXT NOT NULL` | The exact config used for this run |
| `run_type` | `TEXT NOT NULL` | `unit` \| `signal_ic` \| `backtest` \| `walk_forward` \| `paper` \| `live` |
| `status` | `TEXT NOT NULL` | `running` \| `passed` \| `failed` \| `blocked` |
| `data_version` | `TEXT` | MLflow manifest path; required by application logic when `run_type IN ('backtest', 'walk_forward')` (C7) |
| `metrics` | `JSONB NOT NULL DEFAULT '{}'` | Flexible metrics bag: sharpe, annualized_return, max_drawdown, IC, etc. |
| `artifact_path` | `TEXT` | Path to any output artifact (tearsheet, blotter, etc.) |
| `mlflow_run_id` | `TEXT` | |
| `notes` | `TEXT` | |
| `started_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `completed_at` | `TIMESTAMPTZ` | NULL while `status = 'running'` |

**Foreign key:** `(strategy_id, config_hash)` → `strategy_definitions(strategy_id, config_hash)` ON DELETE RESTRICT

**Constraints:**
- `ck_strategy_runs_run_type`: `run_type IN ('unit', 'signal_ic', 'backtest', 'walk_forward', 'paper', 'live')`
- `ck_strategy_runs_status`: `status IN ('running', 'passed', 'failed', 'blocked')`

**Indexes:**
- `ix_strategy_runs_strategy_started` on `(strategy_id, started_at DESC)`
- `ix_strategy_runs_type_status` on `(run_type, status)`

---

### 4.4 `strategy_status_history`

Append-only audit trail of every lifecycle transition on a formally registered strategy. Never updated or deleted (C3).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL PRIMARY KEY` | |
| `strategy_id` | `TEXT NOT NULL` | FK → `strategies(strategy_id)` ON DELETE RESTRICT |
| `from_status` | `TEXT` | NULL for the initial registration event |
| `to_status` | `TEXT NOT NULL` | |
| `transitioned_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `operator_notes` | `TEXT` | Required when `to_status = 'live'` |

**Index:** `ix_strategy_status_history_strategy_id` on `(strategy_id, transitioned_at DESC)`

---

## 5. Canonical Config Hashing

The config hash is computed over the **canonical form** of the YAML, not the raw file bytes. This means:

1. YAML is parsed to a dict
2. Keys matching `_RUNTIME_KEYS = {"data_version"}` are recursively stripped (these are dataset pins that change between runs but don't affect strategy logic)
3. The remaining dict is serialised as JSON with `sort_keys=True`, `separators=(',', ':')` (no whitespace)
4. SHA-256 of the UTF-8 encoded JSON string

**Effect:** Changing only `data_version` in the YAML does not produce a new config hash. Changing any factor weight, portfolio parameter, or execution setting does.

This is the correct behaviour for C6: the guard is against changing *strategy logic*, not against updating the dataset pin for a new backtest run.

---

## 6. Python Interface

### 6.1 Module layout

```
strategy_registry/
├── __init__.py       # re-exports StrategyRegistry, StrategyStatus
├── models.py         # SQLAlchemy ORM: StrategyDefinition, Strategy,
│                     #   StrategyRun, StrategyStatusHistory
├── fingerprint.py    # canonical hashing, YAML validation, ID normalisation
├── registry.py       # StrategyRegistry class — all business logic
└── cli.py            # CLI: python -m strategy_registry <subcommand>
```

`loader.py` is replaced by `fingerprint.py` which adds canonical hashing and config validation (ported from `local/strategies`).

### 6.2 `StrategyStatus` enum

```python
class StrategyStatus(str, enum.Enum):
    BACKTESTING = "backtesting"
    PAPER       = "paper"
    LIVE        = "live"
    ARCHIVED    = "archived"
```

### 6.3 `StrategyRegistry` API

```python
class StrategyRegistry:
    def __init__(self, db_url: str) -> None: ...

    # ── Definition layer ────────────────────────────────────────────────────

    def fingerprint(self, config_path: str) -> StrategyDefinition:
        """
        Validate and canonically hash a strategy YAML. Does NOT write to DB.
        Use this to check a config before committing to registration.
        """

    def add_definition(
        self,
        config_path: str,
        explicit_strategy_id: str | None = None,
    ) -> StrategyDefinition:
        """
        Validate, hash, and insert a row into strategy_definitions if the
        (strategy_id, config_hash) pair doesn't already exist.
        Idempotent: calling twice with the same config is a no-op.
        """

    # ── Lifecycle layer ──────────────────────────────────────────────────────

    def register(
        self,
        config_path: str,
        strategy_family: str | None = None,
        supersedes_strategy_id: str | None = None,
        notes: str | None = None,
        explicit_strategy_id: str | None = None,
    ) -> Strategy:
        """
        Formally register a strategy for operational use. Calls add_definition()
        internally, then creates a strategies row with status=backtesting.
        Raises StrategyAlreadyRegisteredError if strategy_id already has a
        strategies row (strategy_id values are permanent).
        """

    def transition(
        self,
        strategy_id: str,
        to_status: StrategyStatus,
        operator_notes: str | None = None,
    ) -> Strategy:
        """
        Validate and execute a status transition. Appends a
        strategy_status_history row. Requires operator_notes for live (C8 path).
        Raises ConflictingActiveStrategyError if another strategy already holds
        the target paper/live slot.
        """

    def get(self, strategy_id: str) -> Strategy: ...

    def list(
        self,
        status: StrategyStatus | None = None,
        strategy_family: str | None = None,
    ) -> list[Strategy]: ...

    def verify_config_integrity(self, strategy_id: str) -> bool:
        """
        Re-fingerprint the YAML at source_path and compare against
        canonical_config_hash. Raises ConfigDriftError if it differs (C6).
        """

    # ── Run recording layer ──────────────────────────────────────────────────

    def record_run(
        self,
        strategy_id: str,
        config_hash: str,
        run_type: str,
        status: str,
        metrics: dict | None = None,
        data_version: str | None = None,
        artifact_path: str | None = None,
        mlflow_run_id: str | None = None,
        notes: str | None = None,
    ) -> StrategyRun:
        """
        Append a run record. Requires data_version for run_type in
        ('backtest', 'walk_forward') — C7. The (strategy_id, config_hash)
        pair must exist in strategy_definitions.
        """

    def get_runs(
        self,
        strategy_id: str,
        run_type: str | None = None,
        status: str | None = None,
    ) -> list[StrategyRun]: ...
```

### 6.4 CLI subcommands

```
# Validate and display fingerprint without writing anything
python -m strategy_registry fingerprint  config/strategy/v1_base_momentum.yaml

# Add a definition (pre-registration; idempotent)
python -m strategy_registry define       config/strategy/v1_base_momentum.yaml
                                         [--strategy-id v1_base_momentum]

# Formally register for operational use
python -m strategy_registry register     config/strategy/v1_base_momentum.yaml
                                         [--strategy-id v1_base_momentum]
                                         [--family base_momentum]
                                         [--supersedes v1_base_momentum]
                                         [--notes "..."]

# Status transitions
python -m strategy_registry status       --strategy-id v1_base_momentum
                                         --to paper
                                         [--notes "..."]

# Listing and inspection
python -m strategy_registry list         [--status paper] [--family base_momentum]
python -m strategy_registry show         --strategy-id v1_base_momentum

# C6 config integrity check
python -m strategy_registry verify       --strategy-id v1_base_momentum

# Record an experiment run
python -m strategy_registry record-run   --strategy-id v1_base_momentum
                                         --config-hash <64-char hex>
                                         --run-type backtest
                                         --status passed
                                         --data-version rqis-snapshots/manifests/2026-06-14/manifest.json
                                         --metrics-json local/backtest_metrics.json
                                         [--mlflow-run-id <id>]
                                         [--artifact-path local/tearsheet.html]

# List runs for a strategy
python -m strategy_registry runs         --strategy-id v1_base_momentum
                                         [--run-type backtest]
                                         [--status passed]
```

---

## 7. Integrity and Safety Rules

| Rule | Mechanism |
|------|-----------|
| C3 — no deletes on audit tables | `strategy_status_history` has no `DELETE` path; `strategy_runs` records with terminal status are append-only |
| C6 — config immutability | `verify_config_integrity()` re-fingerprints the YAML and raises `ConfigDriftError` if canonical hash differs; called by paper/live pipeline before every run |
| C7 — data version in backtest | `record_run()` raises `MissingDataVersionError` when `run_type` is `backtest` or `walk_forward` and `data_version` is empty |
| One paper/live at a time | DB partial unique indexes + Python `ConflictingActiveStrategyError` |
| `archived` is terminal | `transition()` raises `InvalidTransitionError` from archived |
| Live requires notes | `transition(to_status='live')` raises `MissingOperatorNotesError` if `operator_notes` is blank |
| `strategy_id` is permanent | `StrategyAlreadyRegisteredError` if a `strategies` row already exists; use `v{N+1}_…` naming for new versions |
| Strategy ID format | Enforced at DB (`CHECK`) and Python (`re`) level: `^[a-z][a-z0-9_]{2,99}$` |
| Config validation on define/register | Factor weights sum to positive, required sections present, dates ordered, `initial_capital > 0`, `n_long > 0` — fails fast before any DB write |

---

## 8. Integration Points

| System | How Registry Integrates |
|--------|------------------------|
| Paper pipeline (Steps 1–8) | Step 1 calls `registry.verify_config_integrity()` to catch config drift before each run; Step 6 calls `registry.record_run(run_type='paper', ...)` after blotter generation |
| Airflow DAG (M5.4) | DAG start task calls `registry.list(status='paper')` to find the active strategy and its canonical config path |
| Tearsheets (M5.3) | Queries `strategy_runs` for `run_type IN ('backtest', 'paper')` with `status='passed'` to populate performance tables; metrics JSONB is the source |
| Streamlit dashboard (M5.8) | Displays `strategies` + `strategy_runs` side by side; operator triggers transitions via UI |
| MLflow | `mlflow_run_id` on `strategy_runs` rows links back to experiment tracking server |

---

## 9. Out of Scope (Phase 5.1)

- Multi-strategy paper/live mode (single active enforced per mode)
- Updating `canonical_config_hash` after registration (requires new `strategy_id`)
- Strategy scheduling / rotation (M5.4 Airflow DAG)
- Live transitions (C8 requires 4-week paper qualification — not unlocked yet)
