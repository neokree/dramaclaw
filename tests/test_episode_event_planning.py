"""Event-level episode planning on SQLiteStore, with the text engine faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novelvideo.models import NovelEvent

NOVEL = "第一章 雨巷\n\n林昭撑伞走进雨巷。\n\n第二章 钟楼\n\n林昭登上钟楼。\n"


@pytest.mark.asyncio
async def test_build_episodes_from_events_uses_the_text_engine_assignment(
    tmp_path, monkeypatch
):
    from novelvideo import sqlite_store as sqlite_store_module
    from novelvideo.story import event_extractor

    async def fake_extract(self, chapter_num, chapter_content, on_log=None):
        return [
            NovelEvent(
                event_id=f"ch{chapter_num}_e1",
                chapter_num=chapter_num,
                description=f"事件{chapter_num}",
                characters=["林昭"],
                content=chapter_content,
            )
        ]

    prompts: list[str] = []

    class FakeAgent:
        async def run(self, prompt):
            prompts.append(prompt)
            return SimpleNamespace(
                output=sqlite_store_module._EpisodeEventAssignmentList(
                    assignments=[
                        {"episode": 1, "event_ids": ["ch1_e1", "ch2_e1"]},
                    ]
                )
            )

    monkeypatch.setattr(event_extractor.EventExtractor, "extract_events", fake_extract)
    monkeypatch.setattr(sqlite_store_module, "_text_agent", lambda _output: FakeAgent())

    store = sqlite_store_module.SQLiteStore(
        "alice/events", output_dir=str(tmp_path), state_dir=str(tmp_path)
    )
    try:
        await store.initialize()
        store.save_novel_content(NOVEL)

        episodes = await store.build_episodes_from_events(target_episodes=1)

        assert len(prompts) == 1 and "ch2_e1" in prompts[0]
        assert [ep.number for ep in episodes] == [1]
        assert episodes[0].event_ids == ["ch1_e1", "ch2_e1"]
        assert (episodes[0].chapter_start, episodes[0].chapter_end) == (1, 2)
        stored = await store.get_episode_from_graph(1)
        assert stored is not None and stored.key_events == ["事件1", "事件2"]
    finally:
        await store.close()
