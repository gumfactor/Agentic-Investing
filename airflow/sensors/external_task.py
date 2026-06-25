"""Minimal ExternalTaskSensor stub."""
from __future__ import annotations

from typing import Any

from airflow.sensors.base import BaseSensorOperator


class ExternalTaskSensor(BaseSensorOperator):
    def __init__(
        self,
        *,
        external_dag_id: str = "",
        external_task_id: str = "",
        execution_delta: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.external_dag_id = external_dag_id
        self.external_task_id = external_task_id
        self.execution_delta = execution_delta
