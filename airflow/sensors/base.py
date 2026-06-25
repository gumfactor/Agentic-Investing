"""Minimal BaseSensorOperator stub."""
from __future__ import annotations

import structlog
from typing import Any

from airflow import DAG


class BaseSensorOperator:
    def __init__(
        self,
        *,
        task_id: str,
        poke_interval: int = 60,
        timeout: int = 3600,
        mode: str = "poke",
        retries: int = 0,
        soft_fail: bool = False,
        **kwargs: Any,
    ) -> None:
        self.task_id = task_id
        self.poke_interval = poke_interval
        self.timeout = timeout
        self.mode = mode
        self.retries = retries
        self.soft_fail = soft_fail
        dag = DAG._current()
        if dag is not None:
            dag._register(self)
        self.log = structlog.get_logger(type(self).__name__)

    def poke(self, context: Any) -> bool:
        raise NotImplementedError

    def __rshift__(self, other: Any) -> Any:
        return other

    def __rrshift__(self, other: Any) -> "BaseSensorOperator":
        return self
