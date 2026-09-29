"""Task runner for novel/screenplay import."""

from __future__ import annotations

import asyncio
from typing import Any

from novelvideo.i18n_message import MessageLike
from novelvideo.model_gateway_runtime import model_gateway_scope_for_runner
from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.cancel import await_envelope_with_cancel_watch
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_state import get_task_manager


def run_ingest_fast(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    with model_gateway_scope_for_runner(envelope):
        return asyncio.run(
            await_envelope_with_cancel_watch(
                _run_ingest_fast(envelope, ctx),
                envelope,
                task_type="ingest_fast",
            )
        )


async def _run_ingest_fast(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    from novelvideo.sqlite_store import SQLiteStore
    from novelvideo.structured_ingest import ingest_source_text_structured

    payload = envelope.get("payload") or {}
    novel_path = str(payload["novel_path"])
    config = dict(payload.get("config") or {})
    manager = get_task_manager()

    store = SQLiteStore(
        ctx.owner_project_label,
        output_dir=str(ctx.output_dir),
        state_dir=str(ctx.state_dir),
    )
    await store.initialize()

    def update(progress: float | None, task: MessageLike) -> None:
        """Persist a progress milestone or a log-only status update.

        Log messages arrive between the explicit ingest milestones.  A log
        message does not carry progress, so ``None`` preserves the last reported
        value instead of resetting the progress bar to zero.
        """
        manager.update_progress_for_project(
            ctx,
            "ingest_fast",
            0,
            progress=progress,
            current_task=task,
            logs=[task],
        )

    try:
        return await ingest_source_text_structured(
            store,
            novel_path,
            spine_template=str(config.get("spine_template") or "").strip() or None,
            on_progress=update,
            on_log=lambda message: update(None, message),
        )
    finally:
        await store.close()


register_project_task_runner("ingest_fast", run_ingest_fast)
