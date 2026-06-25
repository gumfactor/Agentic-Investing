"""Minimal TimeDeltaSensor stub."""
from __future__ import annotations

from typing import Any

from airflow.sensors.base import BaseSensorOperator


class TimeDeltaSensor(BaseSensorOperator):
    def __init__(self, *, delta: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delta = delta
