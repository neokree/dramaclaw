"""Task runners for the project-level character/scene/prop/episode builds."""

from __future__ import annotations

import asyncio
from typing import Any

from novelvideo.model_gateway_runtime import model_gateway_scope_for_runner
from novelvideo.novel_source import require_imported_novel
from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.cancel import await_envelope_with_cancel_watch
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_state import get_task_manager


def _run_async(coro, envelope: dict[str, Any], task_type: str):
    with model_gateway_scope_for_runner(envelope):
        return asyncio.run(
            await_envelope_with_cancel_watch(coro, envelope, task_type=task_type)
        )


def _progress(
    ctx: ProjectContext, task_type: str, progress: float | None, task: str
) -> None:
    """进度/日志上报。

    `progress=None` 表示「只更新步骤文案和日志，保留原有进度」。普通日志行必须
    走这条路径：构建任务的 on_log 回调本身不带进度，早期用 0.0 占位会让前端进
    度条在 10% → 0% → 80% → 0% 之间反复倒退。
    """
    get_task_manager().update_progress_for_project(
        ctx,
        task_type,
        0,
        progress=progress,
        current_task=task,
        logs=[task],
    )


async def _load_store(ctx: ProjectContext):
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        ctx.owner_project_label,
        output_dir=str(ctx.output_dir),
        state_dir=str(ctx.state_dir),
    )
    await store.initialize()
    await store.load_graph_state()
    return store


def run_build_characters(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return _run_async(_run_build_characters(ctx), envelope, "build_characters")


async def _run_build_characters(ctx: ProjectContext) -> dict[str, Any]:
    require_imported_novel(ctx.output_dir)
    store = await _load_store(ctx)
    try:
        def on_progress(progress: float | None, task: str) -> None:
            _progress(ctx, "build_characters", progress, task)

        def on_log(message: str) -> None:
            _progress(ctx, "build_characters", None, message)

        from novelvideo.structured_builders import build_characters_structured

        added = await build_characters_structured(
            store, on_progress=on_progress, on_log=on_log
        )
        return {"characters": len(added), "added_characters": len(added)}
    finally:
        await store.close()


def run_build_scenes(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return _run_async(_run_build_scenes(ctx), envelope, "build_scenes")


async def _run_build_scenes(ctx: ProjectContext) -> dict[str, Any]:
    require_imported_novel(ctx.output_dir)
    # The API rejects at enqueue; this is the final defence against state races
    # and producers that never went through HTTP. Only a *running* planner
    # blocks, so a build arriving against a queued planner is not turned away
    # for nothing. Two tasks that reach their gates together can still both
    # refuse — narrow, writes nothing, and retrying clears it.
    from novelvideo.scene_prerequisites import (
        ScenePlanningRunningError,
        running_scene_planner,
    )
    from novelvideo.task_state import get_task_manager

    if running_scene_planner(get_task_manager().list_tasks_for_project(ctx)):
        raise ScenePlanningRunningError()
    store = await _load_store(ctx)
    try:
        def on_progress(progress: float | None, task: str) -> None:
            _progress(ctx, "build_scenes", progress, task)

        def on_log(message: str) -> None:
            _progress(ctx, "build_scenes", None, message)

        from novelvideo.structured_builders import build_scenes_structured

        return await build_scenes_structured(
            store, on_progress=on_progress, on_log=on_log
        )
    finally:
        await store.close()


def run_build_props(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return _run_async(_run_build_props(ctx), envelope, "build_props")


async def _run_build_props(ctx: ProjectContext) -> dict[str, Any]:
    store = await _load_store(ctx)
    try:
        def on_progress(progress: float | None, task: str) -> None:
            _progress(ctx, "build_props", progress, task)

        def on_log(message: str) -> None:
            _progress(ctx, "build_props", None, message)

        from novelvideo.structured_builders import build_props_structured

        return await build_props_structured(
            store, on_progress=on_progress, on_log=on_log
        )
    finally:
        await store.close()


def run_build_episodes(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return _run_async(_run_build_episodes(envelope, ctx), envelope, "build_episodes")


async def _run_build_episodes(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    payload = envelope.get("payload") or {}
    config = dict(payload.get("config") or {})
    planning_mode = str(config.get("planning_mode", "chapters"))
    generate_metadata = bool(config.get("generate_metadata", False))
    require_imported_novel(ctx.output_dir)
    # Defence in depth: the route rejects other modes before enqueue, but a
    # task queued by an older build could still carry one.
    # ponytail: SQLiteStore.build_episodes_from_events exists; admit
    # "ai_events" here and in the route if event planning is re-enabled.
    if planning_mode != "chapters":
        raise ValueError(f"Unsupported episode planning mode: {planning_mode}")
    store = await _load_store(ctx)
    try:

        def update(progress: float | None, task: str) -> None:
            _progress(ctx, "build_episodes", progress, task)

        episodes = await store.build_episodes_from_chapters(
            generate_metadata=generate_metadata,
            on_progress=update,
            on_log=lambda message: update(None, message),
        )
        return {"episodes": len(episodes)}
    finally:
        await store.close()


register_project_task_runner("build_characters", run_build_characters)
register_project_task_runner("build_scenes", run_build_scenes)
register_project_task_runner("build_props", run_build_props)
register_project_task_runner("build_episodes", run_build_episodes)
