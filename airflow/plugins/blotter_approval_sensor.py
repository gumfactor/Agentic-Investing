"""BlotterApprovalSensor — Airflow sensor that waits for operator blotter approval.

Satisfies safety rule C1: orders cannot be submitted until a human operator has
reviewed the blotter artifact, selected which orders to submit, and inserted a
row in the blotter_approvals table with a matching SHA-256 checksum.

The sensor raises AirflowException (no retry) on SHA-256 mismatch so that a
tampered blotter artifact is never submitted.
"""

from __future__ import annotations

import os
from typing import Any

from airflow.exceptions import AirflowException
from airflow.sensors.base import BaseSensorOperator
from airflow.utils.context import Context
from sqlalchemy import create_engine, text


class BlotterApprovalSensor(BaseSensorOperator):
    """Wait for operator approval of a blotter artifact.

    Polls blotter_approvals table until a row exists for blotter_run_id
    and confirmed_blotter_sha256 matches the artifact's SHA-256 in XCom.

    Parameters
    ----------
    blotter_run_id_task_id:
        task_id of the task that pushed ``blotter_run_id`` to XCom.
    blotter_sha256_task_id:
        task_id of the task that pushed ``blotter_sha256`` to XCom.
        Defaults to blotter_run_id_task_id (same task typically pushes both).
    """

    template_fields: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        blotter_run_id_task_id: str,
        blotter_sha256_task_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.blotter_run_id_task_id = blotter_run_id_task_id
        self.blotter_sha256_task_id = blotter_sha256_task_id or blotter_run_id_task_id

    def poke(self, context: Context) -> bool:
        ti = context["ti"]
        blotter_run_id: str | None = ti.xcom_pull(
            key="blotter_run_id", task_ids=self.blotter_run_id_task_id
        )
        expected_sha: str | None = ti.xcom_pull(
            key="blotter_sha256", task_ids=self.blotter_sha256_task_id
        )

        if not blotter_run_id:
            raise AirflowException(
                "blotter_run_id XCom not found — build_blotter task may have failed."
            )
        if not expected_sha:
            raise AirflowException(
                "blotter_sha256 XCom not found — build_blotter task may have failed."
            )

        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise AirflowException("DATABASE_URL environment variable not set.")

        engine = create_engine(database_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT selected_order_ids, approved_by, "
                        "confirmed_blotter_sha256, approved_at_utc "
                        "FROM blotter_approvals "
                        "WHERE blotter_run_id = :run_id "
                        "ORDER BY approved_at_utc DESC "
                        "LIMIT 1"
                    ),
                    {"run_id": blotter_run_id},
                ).fetchone()
        finally:
            engine.dispose()

        if row is None:
            self.log.info(
                "No approval found yet for blotter_run_id=%s; will retry.", blotter_run_id
            )
            return False

        actual_sha = row.confirmed_blotter_sha256
        if actual_sha != expected_sha:
            raise AirflowException(
                f"Blotter SHA-256 mismatch — artifact may have been tampered with. "
                f"Expected {expected_sha!r}, recorded {actual_sha!r}. "
                "Do not retry. Investigate before any resubmission attempt."
            )

        self.log.info(
            "Blotter approved by %s at %s (SHA-256 verified).",
            row.approved_by,
            row.approved_at_utc,
        )
        ti.xcom_push(key="selected_order_ids", value=row.selected_order_ids)
        ti.xcom_push(key="approved_by", value=str(row.approved_by))
        ti.xcom_push(key="approved_at_utc", value=str(row.approved_at_utc))
        return True
