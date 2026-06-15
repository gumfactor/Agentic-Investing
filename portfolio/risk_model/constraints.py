"""Portfolio constraint definitions.

A PortfolioConstraints object describes every limit that the optimizer and
compliance layer must respect.  It is intentionally a pure data container —
no DB access, no logging — so it can be constructed in tests without
infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PortfolioConstraints:
    """All limits that govern portfolio construction.

    All weight values are expressed as decimals (0.05 = 5%).
    """

    # ── Position limits ──────────────────────────────────────────────────────
    max_position_weight: float = 0.05      # single name
    min_position_weight: float = 0.001     # minimum non-zero weight
    max_names: int = 100                   # maximum portfolio constituents

    # ── Sector limits ────────────────────────────────────────────────────────
    max_sector_weight: float = 0.25        # GICS sector cap
    sector_map: dict[str, str] = field(default_factory=dict)  # ticker → sector

    # ── Factor exposure limits ───────────────────────────────────────────────
    # Maps factor name → (min_exposure, max_exposure); None = unbounded
    factor_bounds: dict[str, tuple[float | None, float | None]] = field(
        default_factory=dict
    )

    # ── Turnover / liquidity ─────────────────────────────────────────────────
    max_turnover: float = 1.0              # max one-way turnover per rebalance
    min_adv_fraction: float = 0.01        # max 1% of 30-day ADV in a single day

    # ── Portfolio-level ──────────────────────────────────────────────────────
    target_volatility: float | None = None   # if set, scale weights to hit this
    max_portfolio_beta: float = 1.5          # vs. benchmark
    allow_short: bool = False

    def validate(self) -> None:
        """Raise ValueError on obviously invalid constraint combinations."""
        if not (0.0 < self.max_position_weight <= 1.0):
            raise ValueError(f"max_position_weight={self.max_position_weight} not in (0, 1]")
        if not (0.0 < self.max_sector_weight <= 1.0):
            raise ValueError(f"max_sector_weight={self.max_sector_weight} not in (0, 1]")
        if self.max_names < 1:
            raise ValueError("max_names must be ≥ 1")

    @classmethod
    def from_config(cls, cfg: dict) -> PortfolioConstraints:
        """Build from a config dict (e.g. settings.yaml portfolio section)."""
        return cls(
            max_position_weight=cfg.get("max_position_weight", 0.05),
            max_sector_weight=cfg.get("max_sector_weight", 0.25),
            max_names=cfg.get("max_names", 100),
            target_volatility=cfg.get("target_volatility"),
            max_portfolio_beta=cfg.get("max_portfolio_beta", 1.5),
        )
