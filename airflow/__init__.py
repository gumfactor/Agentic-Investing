"""Minimal Airflow stubs for local testing without apache-airflow installed."""
from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

_dag_stack: list["DAG"] = []
_dag_lock = threading.local()


class DAG:
    """Minimal DAG stub supporting context-manager task registration."""

    def __init__(
        self,
        dag_id: str,
        *,
        schedule_interval: str | None = None,
        start_date: Any = None,
        catchup: bool = True,
        max_active_runs: int = 16,
        default_args: dict | None = None,
        params: dict | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.dag_id = dag_id
        self.schedule_interval = schedule_interval
        self.start_date = start_date
        self.catchup = catchup
        self.max_active_runs = max_active_runs
        self.default_args = default_args or {}
        self.params = params or {}
        self.tags = tags or []
        self.tasks: list[Any] = []

    def __enter__(self) -> "DAG":
        if not hasattr(_dag_lock, "stack"):
            _dag_lock.stack = []
        _dag_lock.stack.append(self)
        return self

    def __exit__(self, *args: Any) -> None:
        if hasattr(_dag_lock, "stack") and _dag_lock.stack:
            _dag_lock.stack.pop()

    def _register(self, task: Any) -> None:
        self.tasks.append(task)

    @staticmethod
    def _current() -> "DAG | None":
        if hasattr(_dag_lock, "stack") and _dag_lock.stack:
            return _dag_lock.stack[-1]
        return None
