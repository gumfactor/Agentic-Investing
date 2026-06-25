"""Minimal PythonOperator stub."""
from __future__ import annotations

from typing import Any, Callable

from airflow import DAG


class PythonOperator:
    def __init__(
        self,
        *,
        task_id: str,
        python_callable: Callable,
        trigger_rule: str = "all_success",
        retries: int = 0,
        execution_timeout: Any = None,
        provide_context: bool = False,
        **kwargs: Any,
    ) -> None:
        self.task_id = task_id
        self.python_callable = python_callable
        self.trigger_rule = trigger_rule
        self.retries = retries
        self.execution_timeout = execution_timeout
        dag = DAG._current()
        if dag is not None:
            dag._register(self)

    def __rshift__(self, other: Any) -> Any:
        return other

    def __rrshift__(self, other: Any) -> "PythonOperator":
        return self
