from pathlib import Path


def test_import_novel_closes_store_after_success(monkeypatch, tmp_path: Path) -> None:
    from novelvideo import cli, structured_builders, structured_ingest

    novel = tmp_path / "novel.txt"
    novel.write_text("第一章 钟楼\n\n林昭走进钟楼。", encoding="utf-8")
    calls: list[str] = []

    class FakeStore:
        def __init__(self, project: str):
            calls.append(f"init:{project}")

        async def initialize(self):
            calls.append("initialize")

        async def load_graph_state(self):
            calls.append("load")

        async def build_episodes_from_chapters(self):
            calls.append("episodes")
            return [object()]

        async def close(self):
            calls.append("close")

    async def fake_ingest(store, novel_path):
        calls.append(f"ingest:{Path(novel_path).name}")
        return {"char_count": 7}

    async def fake_characters(store):
        calls.append("characters")
        return ["林昭"]

    monkeypatch.setattr(cli, "SQLiteStore", FakeStore)
    monkeypatch.setattr(cli, "ensure_project_dirs", lambda _project: None)
    monkeypatch.setattr(cli, "_ensure_nest_asyncio", lambda: None)
    monkeypatch.setattr(structured_ingest, "ingest_source_text_structured", fake_ingest)
    monkeypatch.setattr(structured_builders, "build_characters_structured", fake_characters)

    cli.import_novel(project="foss_e2e", novel=str(novel))

    assert calls == [
        "init:foss_e2e",
        "initialize",
        "ingest:novel.txt",
        "load",
        "characters",
        "episodes",
        "close",
    ]
