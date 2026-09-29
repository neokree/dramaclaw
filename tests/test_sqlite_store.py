"""存储层重构验证测试 — SQLite + Kuzu。

验证点：
1. 导入不报错
2. 创建项目 → data.db 生成，3 张表存在
3. novel.txt 写入/读回
4. characters 表写入，内存缓存同步
5. episodes 表 + beats 表写入
6. beats 更新正常
7. load_graph_state 从 SQLite 恢复缓存
"""

import asyncio
import json
from pathlib import Path

import pytest


# ── 1. 导入不报错 ──────────────────────────────────────────
def test_import():
    from novelvideo.models import NovelCharacter
    from pydantic import BaseModel

    assert issubclass(NovelCharacter, BaseModel)
    # 确认没有 DataPoint 基类
    for cls in NovelCharacter.__mro__:
        assert "DataPoint" not in cls.__name__


# ── Fixture: 临时项目目录 + store ─────────────────────────
@pytest.fixture
async def tmp_project(tmp_path):
    """创建临时项目目录和 SQLiteStore。"""
    from novelvideo.sqlite_store import SQLiteStore

    project_dir = tmp_path / "testuser" / "testproject"
    project_dir.mkdir(parents=True)

    store = SQLiteStore(
        "testuser/testproject",
        output_dir=str(project_dir),
        state_dir=str(project_dir),
    )

    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_scene_round_trip_and_update_with_structured_scene_axes(tmp_path):
    from novelvideo.models import NovelScene
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        "admin/demo",
        output_dir=str(tmp_path / "output"),
        state_dir=str(tmp_path / "state"),
    )
    try:
        await store.add_scene(
            NovelScene(
                name="故宫_下雪",
                base_scene_id="故宫",
                variant_id="下雪",
                time_of_day="夜晚",
                aliases=["故宫"],
                scene_type="exterior",
                environment_prompt="雪中的故宫",
                variant_prompt="只改变积雪厚度和宫墙上的冰棱",
            )
        )

        found = await store.get_scene("故宫_下雪")
        assert found is not None
        assert found.base_scene_id == "故宫"
        assert found.variant_id == "下雪"
        assert found.time_of_day == "夜晚"
        assert found.variant_prompt == "只改变积雪厚度和宫墙上的冰棱"

        await store.update_scene(
            "故宫_下雪",
            environment_prompt="暴雪中的故宫",
            variant_prompt="更厚的积雪覆盖屋檐",
            time_of_day="黄昏",
        )
        updated = await store.get_scene("故宫_下雪")
        assert updated is not None
        assert updated.environment_prompt == "暴雪中的故宫"
        assert updated.variant_prompt == "更厚的积雪覆盖屋檐"
        assert updated.time_of_day == "黄昏"

        listed = await store.list_scenes()
        assert [scene.name for scene in listed] == ["故宫_下雪"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_duplicate_main_repair_works_without_graph_cache(tmp_path):
    """The lightweight character list must be able to repair legacy rows."""

    from novelvideo.api.routes.characters import _repair_duplicate_main_characters
    from novelvideo.models import NovelCharacter
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        "admin/demo",
        output_dir=str(tmp_path / "output"),
        state_dir=str(tmp_path / "state"),
    )
    try:
        await store.add_character(NovelCharacter(name="甲", is_main=True))
        await store.add_character(NovelCharacter(name="乙", is_main=True))
        store._characters.clear()

        rows = await store.list_characters()
        repaired = await _repair_duplicate_main_characters(store, rows)

        assert [row.name for row in repaired if row.is_main] == ["甲"]
        persisted = {row.name: row for row in await store.list_characters()}
        assert persisted["甲"].is_main is True
        assert persisted["乙"].is_main is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_add_scene_is_idempotent_by_scene_name(tmp_path):
    from novelvideo.models import NovelScene
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        "admin/demo",
        output_dir=str(tmp_path / "output"),
        state_dir=str(tmp_path / "state"),
    )
    try:
        await store.add_scene(NovelScene(name="故宫"))
        await store.add_scene(NovelScene(name="皇宫"))

        await store.add_scene(
            NovelScene(
                name="故宫_下雪",
                environment_prompt="初版雪景",
            )
        )
        await store.add_scene(
            NovelScene(
                name="故宫_下雪",
                environment_prompt="更新后的暴雪",
            )
        )

        scenes = await store.list_scenes()
        derived = [scene for scene in scenes if scene.name == "故宫_下雪"]
        assert len(derived) == 1
        assert derived[0].name == "故宫_下雪"
        assert derived[0].environment_prompt == "更新后的暴雪"

        assert sorted(scene.name for scene in scenes) == ["故宫", "故宫_下雪", "皇宫"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_add_scene_concurrent_same_name_keeps_single_row(tmp_path):
    from novelvideo.models import NovelScene
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        "admin/demo",
        output_dir=str(tmp_path / "output"),
        state_dir=str(tmp_path / "state"),
    )
    try:
        await store.add_scene(NovelScene(name="故宫"))

        await asyncio.gather(
            store.add_scene(
                NovelScene(
                    name="故宫_下雪",
                    description="worker a",
                )
            ),
            store.add_scene(
                NovelScene(
                    name="故宫_下雪",
                    description="worker b",
                )
            ),
        )

        scenes = await store.list_scenes()
        derived = [scene for scene in scenes if scene.name == "故宫_下雪"]
        assert len(derived) == 1
        assert derived[0].name == "故宫_下雪"
        assert derived[0].description in {"worker a", "worker b"}
    finally:
        await store.close()


def test_compose_derived_scene_name_uses_ascii_underscore():
    from novelvideo.utils.derived_scenes import (
        compose_derived_scene_name,
        derived_scene_ids,
        resolve_base_of,
    )

    assert compose_derived_scene_name("故宫", "下雪") == "故宫_下雪"
    assert compose_derived_scene_name("皇宫_大殿", "雪_夜") == "皇宫_大殿_雪_夜"
    names = {"故宫", "故宫_下雪", "故宫_西配殿", "故宫_西配殿_下雪", "御花园"}
    assert derived_scene_ids("故宫", names) == {
        "故宫_下雪",
        "故宫_西配殿",
        "故宫_西配殿_下雪",
    }
    assert resolve_base_of("故宫_西配殿_下雪", names) == "故宫_西配殿"
    assert resolve_base_of("御花园", names) == "御花园"


# ── 2. data.db 生成 + 3 张表 ───────────────────────────────
@pytest.mark.asyncio
async def test_db_creation(tmp_project):
    store = tmp_project
    db = await store._ensure_db()

    # data.db 文件存在
    assert Path(store.db_path).exists()

    # 3 张表
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") as cur:
        tables = [r[0] for r in await cur.fetchall()]

    assert "beats" in tables
    assert "characters" in tables
    assert "episodes" in tables


# ── 3. novel.txt 写入/读回 ─────────────────────────────────
@pytest.mark.asyncio
async def test_novel_content(tmp_project):
    store = tmp_project
    content = "这是一段测试小说内容，用于验证存储层重构。" * 100

    store.save_novel_content(content)

    novel_path = Path(store.project_dir) / "novel.txt"
    assert novel_path.exists()

    loaded = store.load_novel_content()
    assert loaded == content


# ── 4. characters 写入 + 内存缓存 ──────────────────────────
@pytest.mark.asyncio
async def test_character_crud(tmp_project):
    from novelvideo.models import NovelCharacter

    store = tmp_project
    await store._ensure_db()

    char = NovelCharacter(
        name="苏清晏",
        aliases=["清晏", "苏大小姐"],
        role="女主",
        is_main=True,
        gender="女",
        body_type="纤细高挑",
        description="温婉聪慧的大家闺秀",
        face_prompt="女性，二十出头，黑色长发，杏眼，白皙肤色",
    )

    await store.add_character(char)

    # 内存缓存
    assert "苏清晏" in store._characters
    assert store._alias_index.get("清晏") == "苏清晏"

    # 查询
    found = store.get_character("清晏")
    assert found is not None
    assert found.name == "苏清晏"
    assert found.gender == "女"

    # 更新
    await store.update_character("苏清晏", description="冰雪聪明的嫡女")
    assert store.get_character("苏清晏").description == "冰雪聪明的嫡女"

    # list 从 SQLite 读
    chars = await store.list_characters()
    assert len(chars) == 1
    assert chars[0].name == "苏清晏"
    assert chars[0].aliases == ["清晏", "苏大小姐"]

    # 删除
    await store.delete_character("苏清晏")
    assert store.get_character("苏清晏") is None
    chars = await store.list_characters()
    assert len(chars) == 0


# ── 5. episodes + beats 写入 ───────────────────────────────
@pytest.mark.asyncio
async def test_episode_and_beats(tmp_project):
    from novelvideo.models import NovelEpisode, NovelVisualBeat

    store = tmp_project
    await store._ensure_db()

    # 添加剧集
    ep = NovelEpisode(
        number=1,
        title="第一集",
        content_summary="测试摘要",
        key_events=["事件1", "事件2"],
        character_names=["苏清晏"],
    )
    await store.add_episodes([ep])

    # 保存剧集原文
    await store.save_episode_content(1, "这是第一集的完整原文内容")
    loaded = await store.load_episode_content(1)
    assert loaded == "这是第一集的完整原文内容"

    # 列出剧集
    episodes = await store.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].title == "第一集"
    assert episodes[0].key_events == ["事件1", "事件2"]

    # 添加 beats
    beats = [
        NovelVisualBeat(
            beat_number=i,
            episode_number=1,
            narration=f"第{i}个节拍的旁白",
            visual_description=f"场景描述{i}",
            time_of_day="黄昏",
        )
        for i in range(3)
    ]
    await store.add_visual_beats(beats)

    # 查询 beats
    loaded_beats = await store.get_beats_for_episode(1)
    assert len(loaded_beats) == 3
    assert loaded_beats[0].time_of_day == "黄昏"

    # get_beats_as_dicts
    dicts = await store.get_beats_as_dicts(1)
    assert len(dicts) == 3
    assert dicts[0]["narration_segment"] == "第0个节拍的旁白"


@pytest.mark.asyncio
async def test_count_beats_by_episode_groups_in_one_query(tmp_project):
    """分集列表的镜头数走一次分组查询，未拆镜的集不出现在结果里。

    这个方法存在的意义就是替掉「前端逐集拉完整 beats 再取 len()」。
    """
    from novelvideo.models import NovelEpisode, NovelVisualBeat

    store = tmp_project
    await store._ensure_db()
    await store.add_episodes([NovelEpisode(number=n, title=f"第{n}集") for n in (1, 2, 3)])

    await store.add_visual_beats(
        [
            NovelVisualBeat(beat_number=i, episode_number=1, narration="", visual_description="")
            for i in range(3)
        ]
        + [
            NovelVisualBeat(beat_number=i, episode_number=3, narration="", visual_description="")
            for i in range(5)
        ]
    )

    counts = await store.count_beats_by_episode()

    assert counts == {1: 3, 3: 5}
    # 第 2 集还没拆镜——缺席而不是 0，路由层用 .get(number, 0) 补齐。
    assert 2 not in counts


@pytest.mark.asyncio
async def test_count_beats_by_episode_on_empty_project(tmp_project):
    store = tmp_project
    await store._ensure_db()

    assert await store.count_beats_by_episode() == {}


@pytest.mark.asyncio
async def test_list_beat_asset_refs_returns_reference_columns_in_order(tmp_project):
    """窄列读回来的六个字段要和完整 beat 对得上，包括 scene_ref 的解码。

    资产反向索引拿它替掉 ``list_visual_beats()``。字段名和 ``NovelVisualBeat``
    一致（含 ``scene_ref`` 属性），扫描代码才能对两种形状一视同仁——这个测试就是
    钉住这条等价性。
    """
    from novelvideo.models import SceneRef
    from novelvideo.models import NovelVisualBeat

    store = tmp_project
    await store._ensure_db()
    await store.add_visual_beats(
        [
            NovelVisualBeat(
                beat_number=2,
                episode_number=1,
                narration="旁白",
                visual_description="[[红酒杯]] 摆在桌上",
                detected_identities_json='["苏清晏_少女"]',
                detected_props_json='["红酒杯"]',
                scene_ref_json=SceneRef(scene_id="书房", variant_id="").model_dump_json(),
            ),
            NovelVisualBeat(
                beat_number=1,
                episode_number=1,
                narration="旁白",
                visual_description="空镜",
            ),
        ]
    )

    rows = await store.list_beat_asset_refs()

    assert [(r.episode_number, r.beat_number) for r in rows] == [(1, 1), (1, 2)]

    first, second = rows
    # 没有场景/身份/道具的 beat 读出来是空值而不是 None，扫描直接 json.loads。
    assert first.detected_identities_json == "[]"
    assert first.detected_props_json == "[]"
    assert first.scene_ref_json == ""
    assert first.scene_id == ""

    assert second.detected_identities_json == '["苏清晏_少女"]'
    assert second.detected_props_json == '["红酒杯"]'
    assert second.visual_description == "[[红酒杯]] 摆在桌上"
    assert second.scene_id == "书房"

    # 和完整读法逐字段等价——两条路径不能对同一行给出不同答案。
    full = {(b.episode_number, b.beat_number): b for b in await store.list_visual_beats()}
    for row in rows:
        beat = full[(row.episode_number, row.beat_number)]
        assert row.visual_description == beat.visual_description
        assert row.detected_identities_json == beat.detected_identities_json
        assert row.detected_props_json == beat.detected_props_json
        assert row.scene_id == beat.scene_id


@pytest.mark.asyncio
async def test_list_beat_asset_refs_on_empty_project(tmp_project):
    store = tmp_project
    await store._ensure_db()

    assert await store.list_beat_asset_refs() == []


@pytest.mark.asyncio
async def test_replace_episodes_rolls_back_delete_when_new_plan_write_fails(
    tmp_path,
    monkeypatch,
):
    from novelvideo.models import NovelEpisode
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        "admin/demo",
        output_dir=str(tmp_path / "output"),
        state_dir=str(tmp_path / "state"),
    )
    old_episode = NovelEpisode(
        number=1,
        title="旧规划",
        content_summary="必须在替换失败后保留。",
    )
    new_episodes = [
        NovelEpisode(number=1, title="新规划一"),
        NovelEpisode(number=2, title="新规划二"),
    ]
    try:
        await store.add_episodes([old_episode])
        original_upsert = store._upsert_episodes

        async def fail_after_partial_write(db, episodes):
            await original_upsert(db, episodes[:1])
            raise RuntimeError("simulated planner persistence failure")

        monkeypatch.setattr(store, "_upsert_episodes", fail_after_partial_write)

        with pytest.raises(RuntimeError, match="simulated planner persistence failure"):
            await store.replace_episodes(new_episodes)

        persisted = await store.list_episodes()
        assert [(episode.number, episode.title) for episode in persisted] == [
            (1, "旧规划")
        ]
        assert store.get_episode(1) is old_episode
        assert store.get_episode(2) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_replace_episodes_commits_new_plan_and_refreshes_cache(tmp_path):
    from novelvideo.models import NovelEpisode
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        "admin/demo",
        output_dir=str(tmp_path / "output"),
        state_dir=str(tmp_path / "state"),
    )
    replacement = [
        NovelEpisode(number=2, title="第二集"),
        NovelEpisode(number=3, title="第三集"),
    ]
    try:
        await store.add_episodes([NovelEpisode(number=1, title="旧第一集")])

        await store.replace_episodes(replacement)

        persisted = await store.list_episodes()
        assert [(episode.number, episode.title) for episode in persisted] == [
            (2, "第二集"),
            (3, "第三集"),
        ]
        assert store.get_episode(1) is None
        assert store.get_episode(2) is replacement[0]
        assert store.get_episode(3) is replacement[1]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_episode_schema_migration_adds_planning_columns(tmp_path):
    import aiosqlite

    from novelvideo.models import NovelEpisode
    from novelvideo.sqlite_store import SQLiteStore

    output_dir = tmp_path / "output" / "testuser" / "legacy_episode_schema"
    state_dir = tmp_path / "state" / "testuser" / "legacy_episode_schema"
    output_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    db_path = state_dir / "data.db"

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE episodes (
                number INTEGER PRIMARY KEY,
                title TEXT DEFAULT '',
                chapter_start INTEGER DEFAULT 0,
                chapter_end INTEGER DEFAULT 0,
                content_summary TEXT DEFAULT '',
                main_conflict TEXT DEFAULT '',
                cliffhanger TEXT DEFAULT '',
                key_events TEXT DEFAULT '[]',
                character_names TEXT DEFAULT '[]',
                identity_ids TEXT DEFAULT '[]',
                event_ids TEXT DEFAULT '[]',
                sketch_colors_json TEXT DEFAULT '{}',
                raw_content TEXT DEFAULT '',
                adapted_content TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        await db.commit()

    store = SQLiteStore(
        "testuser/legacy_episode_schema",
        output_dir=str(output_dir),
        state_dir=str(state_dir),
    )
    try:
        await store.initialize()
        await store.add_episodes(
            [
                NovelEpisode(
                    number=1,
                    title="第一集",
                    raw_content="完整原文",
                    beat_source_text="逐行分镜文本",
                )
            ]
        )

        episodes = await store.list_episodes()
        assert episodes[0].beat_source_text == "逐行分镜文本"

        async with store._db.execute("PRAGMA table_info(episodes)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        assert {
            "beat_source_text",
            "scene_menu_json",
            "prop_menu_json",
            "identity_default_map_json",
        }.issubset(columns)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_beats_schema_migration_adds_current_columns(tmp_path):
    import aiosqlite

    from novelvideo.models import NovelVisualBeat
    from novelvideo.sqlite_store import SQLiteStore

    output_dir = tmp_path / "output" / "testuser" / "legacy_beats_schema"
    state_dir = tmp_path / "state" / "testuser" / "legacy_beats_schema"
    output_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    db_path = state_dir / "data.db"

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE beats (
                episode_number INTEGER NOT NULL,
                beat_number INTEGER NOT NULL,
                narration TEXT DEFAULT '',
                visual_description TEXT DEFAULT '',
                video_prompt TEXT,
                location TEXT DEFAULT '',
                location_description TEXT DEFAULT '',
                time_of_day TEXT DEFAULT '',
                set_description TEXT DEFAULT '',
                keyframe_prompt TEXT,
                video_mode TEXT DEFAULT 'first_frame',
                detected_identities_json TEXT DEFAULT '[]',
                audio_type TEXT DEFAULT 'narration',
                speaker TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (episode_number, beat_number)
            )"""
        )
        await db.commit()

    store = SQLiteStore(
        "testuser/legacy_beats_schema",
        output_dir=str(output_dir),
        state_dir=str(state_dir),
    )
    try:
        await store.initialize()
        await store.add_visual_beats(
            [
                NovelVisualBeat(
                    episode_number=1,
                    beat_number=1,
                    narration="旁白",
                    visual_description="画面",
                    detected_props_json='["纸箱"]',
                    scene_ref_json='{"scene_id":"客厅"}',
                    speaker_kind="character",
                    shot_order=1,
                    duration_seconds=2.5,
                    is_manual_shot=True,
                )
            ]
        )

        beats = await store.get_beats_for_episode(1)
        assert beats[0].detected_props_json == '["纸箱"]'
        assert json.loads(beats[0].scene_ref_json)["scene_id"] == "客厅"
        assert beats[0].speaker_kind == "character"
        assert beats[0].shot_order == 1
        assert beats[0].duration_seconds == 2.5
        assert beats[0].is_manual_shot is True

        async with store._db.execute("PRAGMA table_info(beats)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        assert {
            "detected_props_json",
            "scene_ref_json",
            "speaker_kind",
            "shot_order",
            "duration_seconds",
            "is_manual_shot",
        }.issubset(columns)
    finally:
        await store.close()


# ── 6. beat 更新 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_beat_update(tmp_project):
    from novelvideo.models import NovelVisualBeat

    store = tmp_project
    await store._ensure_db()

    beat = NovelVisualBeat(
        beat_number=0,
        episode_number=1,
        narration="原始旁白",
        visual_description="原始描述",
    )
    await store.add_visual_beats([beat])

    # 更新 video_prompt
    ok = await store.update_beat_asset(
        episode_number=1,
        beat_number=0,
        video_prompt="camera slowly pans left",
        video_mode="first_frame",
    )
    assert ok is True

    # 验证
    prompts = await store.get_beat_prompts(1, 0)
    assert prompts["video_prompt"] == "camera slowly pans left"
    assert prompts["video_mode"] == "first_frame"

    # 更新 detected_identities
    ok = await store.set_beat_detected_identities(1, {0: ["苏清晏_嫡女日常"]})
    assert ok == 1

    dicts = await store.get_beats_as_dicts(1)
    assert dicts[0]["detected_identities"] == ["苏清晏_嫡女日常"]


# ── 7. load_graph_state 恢复缓存 ──────────────────────────
@pytest.mark.asyncio
async def test_load_graph_state(tmp_project):
    from novelvideo.models import NovelCharacter, NovelEpisode

    store = tmp_project
    await store._ensure_db()

    # 写入数据
    char = NovelCharacter(name="谢铮", aliases=["和尚"], gender="男")
    await store.add_character(char)
    ep = NovelEpisode(number=1, title="第一集")
    await store.add_episodes([ep])

    # 清空内存缓存
    store._characters.clear()
    store._episodes.clear()
    store._alias_index.clear()

    # 重新加载
    await store.load_graph_state()

    assert len(store._characters) == 1
    assert "谢铮" in store._characters
    assert store._alias_index.get("和尚") == "谢铮"
    assert len(store._episodes) == 1
    assert 1 in store._episodes


# ── 8. 身份 CRUD ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_identity_crud(tmp_project):
    from novelvideo.models import NovelCharacter, CharacterIdentity

    store = tmp_project
    await store._ensure_db()

    char = NovelCharacter(name="谢铮", gender="男")
    await store.add_character(char)

    # 添加身份
    identity = CharacterIdentity(
        identity_id="谢铮_皇帝",
        character_name="谢铮",
        identity_name="皇帝",
        appearance_details="龙袍、高冠、帝王威仪",
    )
    await store.add_character_identity("谢铮", identity)

    # 验证
    updated = store.get_character("谢铮")
    assert len(updated.identities) == 1

    # 更新身份
    await store.update_character_identity("谢铮", "谢铮_皇帝", appearance_details="金色龙袍")
    updated = store.get_character("谢铮")
    emperor = updated.get_identity("皇帝")
    assert emperor.appearance_details == "金色龙袍"

    # 删除身份
    await store.delete_character_identity("谢铮", "谢铮_皇帝")
    updated = store.get_character("谢铮")
    assert updated.get_identity("皇帝") is None


# ── 9. 删除全部数据 ──────────────────────────────────────
@pytest.mark.asyncio
async def test_delete_project_data(tmp_project):
    from novelvideo.models import NovelCharacter, NovelEpisode

    store = tmp_project
    await store._ensure_db()

    await store.add_character(NovelCharacter(name="A"))
    await store.add_episodes([NovelEpisode(number=1, title="E1")])

    await store.delete_project_data()

    assert len(store._characters) == 0
    assert len(store._episodes) == 0
    assert len(await store.list_characters()) == 0
    assert len(await store.list_episodes()) == 0


# ── 10. sketch_colors 读写 ────────────────────────────────
@pytest.mark.asyncio
async def test_sketch_colors(tmp_project):
    from novelvideo.models import NovelEpisode

    store = tmp_project
    await store._ensure_db()

    ep = NovelEpisode(number=1, title="E1")
    await store.add_episodes([ep])
    store._episodes[1] = ep

    colors = {"苏清晏_嫡女日常": "red", "谢铮_皇帝": "blue"}
    await store.set_sketch_colors(1, colors)

    # 重新加载
    await store.load_graph_state()
    loaded = store.get_sketch_colors(1)
    assert loaded == colors


# ── 11. v2.0 beat 字段 (time/video_prompt) ─
@pytest.mark.asyncio
async def test_new_beat_columns(tmp_project):
    from novelvideo.models import NovelVisualBeat

    store = tmp_project
    await store._ensure_db()

    beat = NovelVisualBeat(
        beat_number=1,
        episode_number=1,
        narration="测试",
        visual_description="画面",
        time_of_day="夜晚",
        video_prompt="camera slowly pans left",
    )
    await store.add_visual_beats([beat])

    loaded = await store.get_beats_for_episode(1)
    assert len(loaded) == 1
    assert loaded[0].time_of_day == "夜晚"
    assert loaded[0].video_prompt == "camera slowly pans left"

    # get_beats_as_dicts 也包含 v2.0 字段
    dicts = await store.get_beats_as_dicts(1)
    assert dicts[0]["time_of_day"] == "夜晚"
    assert dicts[0]["estimated_duration"] == len("测试") / 4.0
    assert dicts[0]["video_prompt"] == "camera slowly pans left"


# ── 12. beat_number 命名统一 ──────────────────────────────
@pytest.mark.asyncio
async def test_beat_number_naming(tmp_project):
    from novelvideo.models import NovelVisualBeat

    store = tmp_project
    await store._ensure_db()

    beat = NovelVisualBeat(
        beat_number=5,
        episode_number=1,
        narration="第五个节拍",
        visual_description="描述",
    )
    await store.add_visual_beats([beat])

    dicts = await store.get_beats_as_dicts(1)
    assert dicts[0]["beat_number"] == 5


# ── 13. get_script_as_dict ────────────────────────────────
@pytest.mark.asyncio
async def test_get_script_as_dict(tmp_project):
    from novelvideo.models import NovelEpisode, NovelVisualBeat

    store = tmp_project
    await store._ensure_db()

    ep = NovelEpisode(number=1, title="测试集")
    await store.add_episodes([ep])
    store._episodes[1] = ep

    beats = [
        NovelVisualBeat(
            beat_number=i,
            episode_number=1,
            narration=f"旁白{i}",
            visual_description=f"画面{i}",
        )
        for i in range(1, 4)
    ]
    await store.add_visual_beats(beats)

    colors = {"角色A_默认": "red"}
    await store.set_sketch_colors(1, colors)

    result = await store.get_script_as_dict(1)
    assert result is not None
    assert result["episode_number"] == 1
    assert result["title"] == "测试集"
    assert len(result["beats"]) == 3
    assert result["sketch_colors"] == colors

    # 不存在的集数返回 None
    assert await store.get_script_as_dict(999) is None


# ── 14. persist_narration_script ──────────────────────────
@pytest.mark.asyncio
async def test_persist_narration_script(tmp_project):
    from novelvideo.models import NovelEpisode

    store = tmp_project
    await store._ensure_db()

    ep = NovelEpisode(number=1, title="E1")
    await store.add_episodes([ep])
    store._episodes[1] = ep

    # 构造一个 minimal NarrationScript-like 对象，避免 MagicMock 自动伪造未知字段。
    from types import SimpleNamespace

    mock_beat = SimpleNamespace(
        beat_number=1,
        narration_segment="旁白文本",
        visual_description="画面描述",
        time_of_day="黄昏",
        shot_id=None,
        scene_ref=None,
        audio_type="narration",
        speaker="",
        speaker_kind="character",
        video_mode="first_frame",
        video_prompt=None,
        keyframe_prompt="",
    )
    mock_script = SimpleNamespace(episode_number=1, beats=[mock_beat])

    await store.persist_narration_script(mock_script)

    loaded = await store.get_beats_for_episode(1)
    assert len(loaded) == 1
    assert loaded[0].narration == "旁白文本"
    assert loaded[0].time_of_day == "黄昏"


@pytest.mark.asyncio
async def test_persist_narration_script_completes_detected_refs_from_markers(tmp_project):
    from novelvideo.models import (
        CharacterIdentity,
        NovelCharacter,
        NovelEpisode,
        NovelProp,
    )

    store = tmp_project
    await store._ensure_db()

    ep = NovelEpisode(number=1, title="E1")
    await store.add_episodes([ep])
    await store.add_character(
        NovelCharacter(
            name="陆辰",
            identities=[
                CharacterIdentity(
                    identity_id="陆辰_青年时期",
                    character_name="陆辰",
                    identity_name="青年时期",
                ),
            ],
        )
    )
    await store.add_prop(NovelProp(name="羊皮笔记本", marker_color="#a78bfa"))

    from types import SimpleNamespace

    mock_beat = SimpleNamespace(
        beat_number=1,
        narration_segment="旁白文本",
        visual_description="{{陆辰_青年时期}}握着[[羊皮笔记本]]。",
        time_of_day="黄昏",
        shot_id=None,
        scene_ref=None,
        audio_type="narration",
        speaker="",
        speaker_kind="character",
        video_mode="first_frame",
        video_prompt=None,
        keyframe_prompt="",
    )
    mock_script = SimpleNamespace(episode_number=1, beats=[mock_beat])

    await store.persist_narration_script(mock_script)

    loaded = await store.get_beats_for_episode(1)
    assert loaded[0].detected_identities_json == '["陆辰_青年时期"]'
    assert loaded[0].detected_props_json == '["羊皮笔记本"]'


@pytest.mark.asyncio
async def test_persist_beats_from_script_completes_empty_detected_markers(tmp_project):
    from novelvideo.models import NovelEpisode

    store = tmp_project
    await store._ensure_db()

    ep = NovelEpisode(number=1, title="E1")
    await store.add_episodes([ep])

    await store.persist_beats_from_script(
        1,
        [
            {
                "beat_number": 1,
                "narration_segment": "旁白文本",
                "visual_description": "无人空镜。",
            }
        ],
    )

    loaded = await store.get_beats_for_episode(1)
    assert loaded[0].detected_identities_json == '["__NO_CHARACTER__"]'
    assert loaded[0].detected_props_json == '["__NO_PROP__"]'


# ── 15. beats 表结构 ───────────────────────────────
@pytest.mark.asyncio
async def test_beats_schema_uses_current_columns(tmp_project):
    """验证 beats 表只包含当前 2.0 schema 需要的核心列。"""
    store = tmp_project
    await store._ensure_db()

    # 确认表使用 beat_number 列
    db = store._db
    async with db.execute("PRAGMA table_info(beats)") as cursor:
        columns = [row[1] for row in await cursor.fetchall()]
    assert "beat_number" in columns
    assert "beat_index" not in columns
    removed_columns = {"shot" + "_hint", "camera" + "_angle", "visual" + "_hint"}
    assert removed_columns.isdisjoint(columns)
    assert "time_of_day" in columns
    assert "scene_ref_json" in columns
    assert "prop_refs_json" not in columns
    assert "video_prompt" in columns


@pytest.mark.asyncio
async def test_sqlite_store_close_rejects_future_operations(tmp_path):
    from novelvideo.sqlite_store import SQLiteStore, StoreClosedError

    store = SQLiteStore(
        "testuser/testproject",
        output_dir=str(tmp_path / "output" / "testuser" / "testproject"),
        state_dir=str(tmp_path / "state" / "testuser" / "testproject"),
    )
    await store.initialize()
    await store.close()

    assert store.is_closed()
    with pytest.raises(StoreClosedError):
        await store.list_episodes()


@pytest.mark.asyncio
async def test_sqlite_store_close_waits_for_inflight_operation(tmp_path, monkeypatch):
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(
        "testuser/testproject",
        output_dir=str(tmp_path / "output" / "testuser" / "testproject"),
        state_dir=str(tmp_path / "state" / "testuser" / "testproject"),
    )
    await store.initialize()

    original_ensure_db = store._ensure_db
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_ensure_db():
        entered.set()
        await release.wait()
        return await original_ensure_db()

    monkeypatch.setattr(store, "_ensure_db", slow_ensure_db)

    list_task = asyncio.create_task(store.list_episodes())
    await asyncio.wait_for(entered.wait(), timeout=1)

    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0.01)
    assert not close_task.done()

    release.set()
    assert await list_task == []
    await asyncio.wait_for(close_task, timeout=1)
    assert store.is_closed()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
