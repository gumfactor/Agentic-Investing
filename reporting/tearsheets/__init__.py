"""Tearsheet generation for RQIS backtest and paper-trading runs."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from reporting.tearsheets.tearsheet import TearsheetGenerator

__all__ = ["TearsheetGenerator", "generate_tearsheet"]


def generate_tearsheet(
    result: object,
    output_path: str | Path,
    *,
    prices=None,
    title: str = "",
) -> Path:
    """Convenience wrapper: build a TearsheetGenerator and render.

    Format is inferred from ``output_path`` extension:
      • ``.html``         → single self-contained HTML file
      • directory path   → PNG charts saved into that directory

    Parameters
    ----------
    result:      BacktestResult (or any object with the same attributes).
    output_path: Destination file path (``.html``) or directory.
    prices:      Optional long-format prices DataFrame (ticker/date/close)
                 used to draw entry/exit overlays.
    title:       Override for the tearsheet title.
    """
    gen = TearsheetGenerator.from_backtest_result(result, prices=prices, title=title)
    p = Path(output_path)
    if p.suffix.lower() == ".html":
        return gen.render_html(p)
    # Directory output: render all charts as individual PNGs
    gen.render_png_dir(p)
    return p
