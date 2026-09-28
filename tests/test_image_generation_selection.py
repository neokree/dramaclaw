from __future__ import annotations

from pathlib import Path

import pytest

from novelvideo.engines import image as engine
from novelvideo.engines._proc import EngineError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
JPEG = b"\xff\xd8\xff" + b"0" * 16


def test_legacy_selections_map_to_higgsfield():
    from novelvideo.config import (
        get_grid_generation_config,
        infer_image_generation_selection,
        normalize_character_image_selection,
        normalize_image_generation_selection,
    )

    assert normalize_image_generation_selection("newapi_gpt_image2") == "higgsfield:gpt_image_2"
    assert normalize_image_generation_selection("huimeng_image2_official") == "higgsfield:gpt_image_2"
    assert normalize_image_generation_selection("openrouter_nanobanana2") == "higgsfield:nano_banana_flash"
    assert normalize_image_generation_selection("seedream") == "higgsfield:seedream_v5_pro"
    assert normalize_image_generation_selection("drawthings") == "drawthings"
    assert normalize_image_generation_selection("unknown") == "higgsfield:nano_banana_flash"
    assert normalize_character_image_selection("nanobanana") == "higgsfield:nano_banana_flash"
    assert infer_image_generation_selection("higgsfield", "gpt_image_2") == "higgsfield:gpt_image_2"
    assert infer_image_generation_selection("newapi", "higgsfield:z_image") == "higgsfield:z_image"
    assert infer_image_generation_selection("drawthings", "") == "drawthings"
    config = get_grid_generation_config(selection_override="higgsfield:gpt_image_2_5?variant=flare")
    assert (config["provider"], config["model"]) == ("higgsfield", "gpt_image_2_5?variant=flare")


def test_selection_options_come_from_the_media_catalog():
    from novelvideo.config import IMAGE_GENERATION_SELECTIONS, image_generation_selection_options

    options = image_generation_selection_options()
    assert list(options)[:2] == ["drawthings", "higgsfield:gpt_image_2_5?variant=flare"]
    assert options["higgsfield:nano_banana_flash"] == "Nano Banana 2"
    assert IMAGE_GENERATION_SELECTIONS["higgsfield:foo"]["model"] == "foo"
    assert "newapi_gpt_image2" not in IMAGE_GENERATION_SELECTIONS


def test_closest_ratio():
    assert engine.closest_ratio("2:1", ["1:1", "16:9", "21:9"]) == "16:9"
    assert engine.closest_ratio("9:16", ["9:16", "1:1"]) == "9:16"
    assert engine.closest_ratio("auto", ["auto", "1:1"]) == "auto"
    assert engine.closest_ratio("1:1", []) is None


@pytest.fixture
def ledger(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_ledger", lambda project, action, **f: calls.append((action, f)))
    return calls


async def test_drawthings_uses_first_ref_as_init_image_and_costs_nothing(monkeypatch, tmp_path, ledger):
    seen = {}

    async def fake_generate(prompt, out, **kw):
        seen.update(kw, prompt=prompt)
        Path(out).write_bytes(PNG)
        return Path(out)

    monkeypatch.setattr(engine.drawthings, "generate", fake_generate)
    out = tmp_path / "grids" / "g.png"
    data, _, error = await engine.generate_image(
        "drawthings", "cat", refs=[JPEG, PNG], aspect_ratio="16:9", output_path=out
    )

    assert (data, error) == (PNG, "")
    assert out.read_bytes() == PNG
    assert seen["aspect_ratio"] == "16:9" and len(seen["refs"]) == 2
    assert seen["refs"][0].endswith(".jpg")
    assert ledger[0][0] == "record" and ledger[0][1]["cost_estimate"] == 0.0
    assert ledger[0][1]["provider"] == "drawthings"
    assert ledger[-1] == ("update", {"request_id": ledger[0][1]["request_id"], "status": "completed"})


async def test_higgsfield_shapes_request_and_records_paid_credits(monkeypatch, tmp_path, ledger):
    monkeypatch.setattr(
        engine.higgsfield,
        "schema",
        lambda job_type: {
            "params": [
                {"name": "prompt"},
                {"name": "aspect_ratio", "enum": ["1:1", "16:9", "9:16"]},
                {"name": "resolution", "enum": ["1k", "2k"]},
                {"name": "quality", "enum": ["low", "medium"]},
            ]
        },
    )
    seen = {}

    async def fake_generate_with_schema(model, out, **kw):
        seen.update(kw, model=model)
        kw["on_accepted"]("job-1", 4.5)
        Path(out).write_bytes(PNG)
        return Path(out), "job-1", {}, []

    monkeypatch.setattr(engine.higgsfield, "generate_with_schema", fake_generate_with_schema)
    data, _, error = await engine.generate_image(
        "higgsfield:gpt_image_2",
        "cat",
        refs=[("a.png", PNG, "image/png")],
        aspect_ratio="21:9",
        image_size="2K",
        quality="medium",
        usage={"project_output_dir": tmp_path, "task_type": "render_grid"},
    )

    assert (data, error) == (PNG, "")
    assert seen["model"] == "gpt_image_2"
    assert (seen["aspect_ratio"], seen["resolution"]) == ("16:9", "2k")
    assert seen["extra"] == {"quality": "medium"}
    assert len(seen["refs"]) == 1
    record = ledger[0][1]
    assert (record["request_id"], record["cost_estimate"], record["task_type"]) == ("job-1", 4.5, "render_grid")
    assert record["model_name"] == "gpt_image_2"
    assert ledger[-1][1]["status"] == "completed"


async def test_engine_failure_is_returned_and_marked_failed(monkeypatch, ledger):
    async def boom(prompt, out, **kw):
        raise EngineError("Draw Things non raggiungibile")

    monkeypatch.setattr(engine.drawthings, "generate", boom)
    data, _, error = await engine.generate_image("drawthings", "cat")

    assert data is None and "non raggiungibile" in error
    assert ledger[-1][1]["status"] == "failed"
    assert (await engine.generate_image("newapi_gpt_image2", "cat"))[0] is None


async def test_local_quote_prices_higgsfield_and_not_drawthings(monkeypatch):
    from novelvideo.ports.local.credit_quote import LocalCreditQuote

    async def fake_quote(selection, **kw):
        assert selection == "higgsfield:nano_banana_flash" and kw["image_size"] == "1K"
        return 2.5

    monkeypatch.setattr(engine, "quote_credits", fake_quote)
    quote = LocalCreditQuote()
    paid = await quote.generation_credit_quote(
        kind="image", model="nano_banana_flash", params={"size": "1K"}, quantity=2,
        product_surface="mainline",
    )
    free = await quote.generation_credit_quote(
        kind="image", model="drawthings", params={}, quantity=1, product_surface="mainline",
    )
    assert (paid.unit_cost, paid.total_cost, free.total_cost) == (3, 6, 0)


def test_image_ledger_stores_credits(tmp_path):
    from novelvideo import image_request_usage as usage

    usage.record_image_request(
        project_output_dir=tmp_path, request_id="j1", provider="higgsfield",
        model_name="gpt_image_2", task_type="render_grid", scope="s", cost_estimate=4.0,
    )
    usage.update_image_request_status(project_output_dir=tmp_path, request_id="j1", status="completed")
    summary = usage.get_image_usage_summary(project_output_dir=tmp_path)
    assert (summary["total_requests"], summary["total_credits"]) == (1, 4.0)
