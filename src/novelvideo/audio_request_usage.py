"""Project-local audio generation attempt records."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from novelvideo.config import OUTPUT_DIR, STATE_DIR
from novelvideo.sqlite_pragmas import configure_sqlite_connection
from novelvideo.sqlite_schema import ensure_sqlite_schema


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audio_request_usage (
    request_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model_name TEXT,
    task_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    episode INTEGER,
    speaker TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',
    accepted_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    error_message TEXT,
    cost_credits REAL
);
CREATE INDEX IF NOT EXISTS idx_audio_request_usage_scope
ON audio_request_usage(task_type, scope, accepted_at DESC);
"""

_SCHEMA_COMPONENT = "audio_request_usage"
# MIGRATION CONTRACT: increment this whenever _SCHEMA_SQL changes.
_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)


def _initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(audio_request_usage)")}
    if "cost_credits" not in columns:  # v1 -> v2: Higgsfield credits per job
        conn.execute("ALTER TABLE audio_request_usage ADD COLUMN cost_credits REAL")


def get_audio_request_usage_db_path(project_output_dir: str | Path) -> Path:
    project_output_dir = Path(project_output_dir).resolve()
    output_root = Path(OUTPUT_DIR).resolve()
    state_root = Path(STATE_DIR).resolve()
    try:
        rel = project_output_dir.relative_to(output_root)
    except ValueError:
        return (project_output_dir / "data.db").resolve()
    return (state_root / rel / "data.db").resolve()


@contextmanager
def _connect(project_output_dir: str | Path):
    db_path = get_audio_request_usage_db_path(project_output_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_sqlite_schema(
        db_path,
        component=_SCHEMA_COMPONENT,
        version=_SCHEMA_VERSION,
        initialize=_initialize,
    )
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    configure_sqlite_connection(conn, set_journal_mode=False)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_audio_generation_attempt(
    *,
    project_output_dir: str | Path,
    request_id: str,
    provider: str,
    model_name: str,
    task_type: str,
    scope: str,
    episode: int | None = None,
    speaker: str | None = None,
    cost_credits: float | None = None,
) -> None:
    now = datetime.now().isoformat()
    with _connect(project_output_dir) as conn:
        conn.execute(
            """
            INSERT INTO audio_request_usage (
                request_id, provider, model_name, task_type, scope,
                episode, speaker, status, accepted_at, updated_at, cost_credits
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (
                request_id,
                provider,
                model_name,
                task_type,
                scope,
                episode,
                speaker,
                now,
                now,
                cost_credits,
            ),
        )


def update_audio_generation_attempt(
    *,
    project_output_dir: str | Path,
    request_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    now = datetime.now().isoformat()
    completed_at = now if status in {"completed", "failed"} else None
    with _connect(project_output_dir) as conn:
        conn.execute(
            """
            UPDATE audio_request_usage
            SET status = ?,
                updated_at = ?,
                completed_at = COALESCE(?, completed_at),
                error_message = COALESCE(?, error_message)
            WHERE request_id = ?
            """,
            (status, now, completed_at, error_message, request_id),
        )


def count_audio_scope_attempts(
    *,
    project_output_dir: str | Path,
    task_type: str,
    scope: str,
    episode: int | None = None,
) -> int:
    where = ["task_type = ?", "scope = ?"]
    params: list[object] = [task_type, scope]
    if episode is not None:
        where.append("episode = ?")
        params.append(int(episode))
    with _connect(project_output_dir) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM audio_request_usage WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()
    return int(row[0] or 0) if row else 0


class HiggsfieldLedger:
    """Writes each paid Higgsfield audio job to the project ledger as it is accepted.

    `accepted` is the `on_accepted(job_id, credits)` callback of the Higgsfield
    engine; `finish` marks those jobs completed or failed. A failed write is
    logged, never raised: the job is already paid for.
    """

    def __init__(
        self,
        project_output_dir: str | Path,
        *,
        task_type: str,
        scope: str,
        model: str,
        episode: int | None = None,
        speaker: str | None = None,
    ) -> None:
        self.project_output_dir = project_output_dir
        self.task_type, self.scope, self.model = task_type, scope, model
        self.episode, self.speaker = episode, speaker
        self.jobs: list[str] = []

    def accepted(self, job_id: str, credits: float) -> None:
        self.jobs.append(job_id)
        try:
            record_audio_generation_attempt(
                project_output_dir=self.project_output_dir,
                request_id=job_id,
                provider="higgsfield",
                model_name=self.model,
                task_type=self.task_type,
                scope=self.scope,
                episode=self.episode,
                speaker=self.speaker,
                cost_credits=credits,
            )
        except Exception:  # noqa: BLE001
            logger.warning("could not record Higgsfield audio job %s", job_id, exc_info=True)

    def finish(self, error: str | None = None) -> None:
        for job_id in self.jobs:
            try:
                update_audio_generation_attempt(
                    project_output_dir=self.project_output_dir,
                    request_id=job_id,
                    status="failed" if error else "completed",
                    error_message=error,
                )
            except Exception:  # noqa: BLE001
                logger.warning("could not update Higgsfield audio job %s", job_id, exc_info=True)
