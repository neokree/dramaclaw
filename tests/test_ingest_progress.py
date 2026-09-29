"""Ingest progress reporting contracts."""

from pathlib import Path

import pytest

from novelvideo.project_context import ProjectContext

pytestmark = pytest.mark.m07


def _ctx(tmp_path: Path) -> ProjectContext:
    return ProjectContext(
        project_id="proj_ingest",
        project_name="demo",
        owner_type="user",
        owner_id="owner",
        owner_username="alice",
        requester_user_id="editor",
        requester_username="bob",
        requester_principals=(("user", "editor"),),
        effective_role="editor",
        home_node_id="node_a",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )


class _RecordingTaskManager:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update_progress_for_project(self, ctx, task_type, episode, **kwargs):
        self.updates.append({"task_type": task_type, "episode": episode, **kwargs})


class _FakeStore:
    instance: "_FakeStore | None" = None

    def __init__(self, *args, **kwargs) -> None:
        self.initialized = False
        self.closed = False
        type(self).instance = self

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True


async def _fake_ingest(store, novel_path, spine_template=None, on_progress=None, on_log=None):
    on_progress(0.05, "读取并校验原文...")
    on_log("文件读取完成")
    on_progress(0.6, "切分原文...")
    on_log("确定性切分完成")
    on_progress(0.85, "记录分析计划...")
    on_log("分析计划已记录")
    on_progress(1.0, "导入完成")
    return {"status": "imported"}


async def test_ingest_logs_preserve_intermediate_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary log lines must not send the progress bar back to zero."""
    from novelvideo import sqlite_store, structured_ingest
    from novelvideo.task_backend.runners import ingest

    manager = _RecordingTaskManager()
    monkeypatch.setattr(ingest, "get_task_manager", lambda: manager)
    monkeypatch.setattr(sqlite_store, "SQLiteStore", _FakeStore)
    monkeypatch.setattr(structured_ingest, "ingest_source_text_structured", _fake_ingest)

    result = await ingest._run_ingest_fast(
        {"payload": {"novel_path": str(tmp_path / "novel.txt")}}, _ctx(tmp_path)
    )

    assert result == {"status": "imported"}
    assert _FakeStore.instance is not None
    assert _FakeStore.instance.initialized
    assert _FakeStore.instance.closed
    assert [update["progress"] for update in manager.updates] == [
        0.05,
        None,
        0.6,
        None,
        0.85,
        None,
        1.0,
    ]
    assert [
        update["progress"] for update in manager.updates if update["progress"] is not None
    ] == sorted(
        update["progress"]
        for update in manager.updates
        if update["progress"] is not None
    )
