"""The whole-row write and the column patch must normalize menus identically.

Both routes write episode menus; if they disagreed, moving a caller from one to
the other would silently rewrite its menus.
"""

from __future__ import annotations

import json

import pytest

from novelvideo.models import NovelProp, NovelScene


@pytest.fixture
async def stores(tmp_path):
    from novelvideo.models import NovelEpisode
    from novelvideo.sqlite_store import SQLiteStore

    state = tmp_path / "user" / "project"
    state.mkdir(parents=True)
    s = SQLiteStore("user/project", output_dir=str(state), state_dir=str(state))
    await s.initialize()
    await s.load_graph_state()

    await s.add_scene(
        NovelScene(name="郑家别墅客厅", aliases=["郑家客厅", "客厅"])
    )
    await s.add_scene(NovelScene(name="主任办公室"))
    await s.add_prop(
        NovelProp(
            name="怀表",
            aliases=["金怀表"],
            prop_type="object",
            visual_prompt="一只旧金怀表",
            description="祖传怀表",
            owner="郑玉琴",
        )
    )
    await s.load_graph_state()
    await s.add_episodes([NovelEpisode(number=1, title="第一集")])

    try:
        yield s, s
    finally:
        await s.close()


async def _row(store, column: str):
    db = await store._ensure_db()
    async with db.execute(f"SELECT {column} FROM episodes WHERE number = 1") as cur:
        return json.loads((await cur.fetchone())[0] or "[]")


MENUS = [
    # An alias, resolved to the canonical scene name.
    {"scene_menu": [{"scene_id": "郑家客厅"}]},
    # Case and spacing variants of the same alias.
    {"scene_menu": [{"scene_id": " 客厅 "}]},
    # A derived scene keeps its base and variant.
    {
        "scene_menu": [
            {"scene_id": "郑家别墅客厅_暴雨版", "base_scene_id": "郑家别墅客厅",
             "variant_id": "暴雨版"}
        ]
    },
    # Duplicates collapse.
    {"scene_menu": [{"scene_id": "主任办公室"}, {"scene_id": "主任办公室"}]},
    # An unknown scene passes through untouched.
    {"scene_menu": [{"scene_id": "查无此地"}]},
    # A prop alias resolves and backfills type, prompt, description and owner.
    {"prop_menu": [{"prop_id": "金怀表"}]},
    # Duplicate props collapse.
    {"prop_menu": [{"prop_id": "怀表"}, {"prop_id": "怀表"}]},
    # An unknown prop keeps whatever the caller supplied.
    {"prop_menu": [{"prop_id": "长剑", "prop_type": "weapon"}]},
    # Emptying a menu is a real update, not a no-op.
    {"scene_menu": []},
    {"prop_menu": []},
]


@pytest.mark.parametrize("menu", MENUS, ids=range(len(MENUS)))
async def test_patch_and_legacy_write_produce_identical_menus(stores, menu):
    legacy, sqlite = stores
    column = "scene_menu_json" if "scene_menu" in menu else "prop_menu_json"

    await legacy.update_episode(1, **menu)
    from_legacy = await _row(sqlite, column)

    await sqlite.patch_episode(1, **menu)
    from_patch = await _row(sqlite, column)

    assert from_patch == from_legacy


async def test_the_patch_leaves_the_other_menu_alone(stores):
    """The whole point: writing one menu must not re-serialise the other."""
    _, sqlite = stores
    await sqlite.patch_episode(1, scene_menu=[{"scene_id": "主任办公室"}])
    await sqlite.patch_episode(1, prop_menu=[{"prop_id": "怀表"}])

    assert await _row(sqlite, "scene_menu_json")
    assert await _row(sqlite, "prop_menu_json")


# ── the race, from every writer ─────────────────────────────────────────────


async def test_editing_an_unrelated_field_leaves_the_menus_alone(stores):
    """A whole-row write loses menus even when it sets a different field.

    Editing a title re-serialises every column from whatever the editor loaded
    earlier, so planning results that landed in between disappear. This is why
    plain field edits go through the patch too.
    """
    _, sqlite = stores
    await sqlite.patch_episode(1, scene_menu=[{"scene_id": "主任办公室"}])
    await sqlite.patch_episode(1, prop_menu=[{"prop_id": "怀表"}])

    await sqlite.patch_episode(1, title="改名后的标题")

    assert await _row(sqlite, "scene_menu_json")
    assert await _row(sqlite, "prop_menu_json")
    episode = await sqlite.get_episode_from_graph(1)
    assert episode.title == "改名后的标题"


async def test_an_identity_cascade_leaves_the_menus_alone(stores):
    """Renaming an identity can happen while planning runs."""
    from novelvideo.models import CharacterIdentity, NovelCharacter

    legacy, sqlite = stores
    await sqlite.add_character(
        NovelCharacter(
            name="林默",
            identities=[CharacterIdentity(identity_id="林默_default", character_name="林默", identity_name="default")],
        )
    )
    await sqlite.load_graph_state()
    await sqlite.patch_episode(
        1,
        identity_ids=["林默_default"],
        scene_menu=[{"scene_id": "主任办公室"}],
        prop_menu=[{"prop_id": "怀表"}],
    )

    await sqlite._cascade_identity_change("林默_default", "林默_雨夜")

    episode = await sqlite.get_episode_from_graph(1)
    assert episode.identity_ids == ["林默_雨夜"]
    assert await _row(sqlite, "scene_menu_json"), "the cascade wiped the scene menu"
    assert await _row(sqlite, "prop_menu_json"), "the cascade wiped the prop menu"


async def test_an_unknown_field_is_refused_rather_than_dropped(stores):
    """Silently ignoring a field would look like a successful write."""
    _, sqlite = stores
    with pytest.raises(ValueError, match="cannot write"):
        await sqlite.patch_episode(1, no_such_column="x")


async def test_the_legacy_facade_can_cascade_an_identity_rename(stores):
    """The cascade runs on the facade, not only on the plain store.

    Testing the SQLiteStore path alone let a missing facade method through:
    every focused test passed while renaming an identity on a legacy project
    raised AttributeError.
    """
    from novelvideo.models import CharacterIdentity, NovelCharacter

    legacy, sqlite = stores
    await sqlite.add_character(
        NovelCharacter(
            name="林默",
            identities=[
                CharacterIdentity(
                    identity_id="林默_default",
                    character_name="林默",
                    identity_name="default",
                )
            ],
        )
    )
    await legacy.load_graph_state()
    await sqlite.patch_episode(
        1,
        identity_ids=["林默_default"],
        scene_menu=[{"scene_id": "主任办公室"}],
    )
    await legacy.load_graph_state()

    await legacy._cascade_identity_change("林默_default", "林默_雨夜")

    episode = await sqlite.get_episode_from_graph(1)
    assert episode.identity_ids == ["林默_雨夜"]
    assert await _row(sqlite, "scene_menu_json"), "the cascade wiped the scene menu"
    # The facade's cached copy must reflect its own write.
    assert legacy.get_episode(1).identity_ids == ["林默_雨夜"]
