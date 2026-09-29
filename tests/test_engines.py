import time

import pytest

from novelvideo.engines import drawthings, h3c, higgsfield
from novelvideo.engines._proc import EngineError


def test_higgsfield_job_id_from_every_create_shape():
    assert higgsfield.job_id_of('[\n  "2dfb8cb8-2338"\n]') == "2dfb8cb8-2338"
    assert higgsfield.job_id_of('{"id": "a"}') == "a"
    assert higgsfield.job_id_of('{"job_ids": ["b"]}') == "b"
    assert higgsfield.job_id_of('{"jobs": [{"id": "c"}]}') == "c"
    assert higgsfield.job_id_of('["  "]') is None
    assert higgsfield.job_id_of("not json") is None


def test_higgsfield_outcome():
    assert higgsfield._outcome({"status": "completed", "result_url": "u"}) == ("done", "u")
    assert higgsfield._outcome([{"status": "completed"}]) == ("failed", None)
    assert higgsfield._outcome({"status": "nsfw"}) == ("failed", None)
    assert higgsfield._outcome({"status": "in_progress"}) == ("running", None)


def test_h3_canvas_frames_seed():
    assert h3c.canvas("9:16") == (576, 1024)
    assert h3c.canvas("16:9") == (1024, 576)
    assert h3c.canvas("4:5") == (896, 1120)
    assert [h3c.frames(s) for s in (0.1, 0.9, 1, 4)] == [22, 22, 39, 107]
    assert h3c.seed_of("block.v1") == h3c.seed_of("block.v1") != h3c.seed_of("block.v2")


def test_drawthings_size_is_multiple_of_64():
    w, h = drawthings.size_for("9:16")
    assert w % 64 == 0 and h % 64 == 0 and w < h
    assert drawthings.size_for("1:1") == (1024, 1024)


def test_higgsfield_job_credits_are_recorded(tmp_path, monkeypatch):
    import asyncio

    from novelvideo.generators import video_generator as vg
    from novelvideo.video_request_usage import get_video_usage_summary

    async def fake_generate_with_schema(model, output_path, *, on_accepted, **_):
        on_accepted("job-1", 6.0)
        return output_path, "job-1", {"duration": 5}, []

    monkeypatch.setattr(vg.higgsfield, "generate_with_schema", fake_generate_with_schema)
    result = asyncio.run(
        vg.HiggsfieldVideoGenerator(model="seedance_2_0").generate(
            None, "p", str(tmp_path / "a.mp4"),
            project_output_dir=str(tmp_path), episode=1, beat_num=2, task_type="single_video",
        )
    )
    assert result.status is vg.VideoGenStatus.DONE
    summary = get_video_usage_summary(project_output_dir=tmp_path)
    assert summary["total_requests"] == 1 and summary["total_credits"] == 6.0


# --------------------------------------------------------------------------
# Schema-driven shaping, against real schemas in tests/fixtures/higgsfield
# --------------------------------------------------------------------------

DEFAULT_BACKEND = "higgsfield:seedance_2_0?mode=fast"


def _schema(job_type):
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures/higgsfield" / f"{job_type}.json").read_text())


def test_higgsfield_shape_params_follow_the_schema():
    kling, seedance = _schema("kling3_0"), _schema("seedance_2_0")

    assert higgsfield.shape_params(
        kling, prompt="p", aspect_ratio="9:16", duration=5.2, resolution="720p",
        generate_audio=False, extra={"mode": "pro"},
    ) == {"prompt": "p", "aspect_ratio": "9:16", "duration": 6, "sound": "off", "mode": "pro"}
    with pytest.raises(EngineError, match="16:9, 9:16, 1:1"):
        higgsfield.shape_params(kling, prompt="p", aspect_ratio="4:5")

    fast = higgsfield.shape_params(seedance, prompt="p", resolution="720p", generate_audio=True)
    assert (fast["mode"], fast["resolution"], fast["generate_audio"]) == ("fast", "720p", True)
    # fast does not render 1080p: the model's std mode does
    assert higgsfield.shape_params(seedance, prompt="p", resolution="1080p")["mode"] == "std"


def test_higgsfield_shape_media_drops_what_the_model_does_not_take():
    kling, seedance = _schema("kling3_0"), _schema("seedance_2_0")

    media, notes = higgsfield.shape_media(
        kling, start_image="a", end_image="b", refs=["r"], video_refs=["v"]
    )
    assert (media["start_image"], media["end_image"], media["refs"], media["video_refs"]) == (
        "a", "b", [], []
    )
    assert notes == ["image_references ignorati", "video_references ignorati"]
    assert higgsfield.shape_media(kling, end_image="b")[1] == ["ultimo fotogramma ignorato"]

    media, notes = higgsfield.shape_media(seedance, start_image="a", refs=[str(i) for i in range(10)])
    assert len(media["refs"]) == 8  # 9 images, the first frame included
    assert notes == ["2 immagini di riferimento oltre il limite di 9"]
    media, _ = higgsfield.shape_media(seedance, audio_refs=["x.wav"])
    assert media["audio_refs"] == []  # audio needs an image or a video


def test_higgsfield_image_limit_and_auto_mode():
    assert higgsfield.image_limit(_schema("seedance_2_0")) == 9
    assert higgsfield.image_limit(_schema("seedance_2_5")) == 30
    assert higgsfield.image_limit(_schema("kling3_0")) is None

    seedance25 = higgsfield.param_specs(_schema("seedance_2_5"))["mode"]["enum"]
    assert higgsfield._auto_mode(seedance25, False, False) == "t2v"
    assert higgsfield._auto_mode(seedance25, True, False) == "omni_reference"
    omni = ["text-to-video", "image-to-video", "reference-to-video"]
    assert higgsfield._auto_mode(omni, True, False) == "image-to-video"
    assert higgsfield._auto_mode(omni, True, True) == "reference-to-video"
    assert higgsfield._auto_mode(["std", "pro"], True, True) is None


def test_higgsfield_schema_ignores_a_model_ref_preset(higgsfield_catalog):
    assert higgsfield.schema("kling3_0?mode=pro")["job_type"] == "kling3_0"


def test_higgsfield_video_generator_shapes_the_request(higgsfield_catalog, tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path

    from novelvideo.generators import video_generator as vg

    sent = []

    async def fake_generate(job_type, params, output_path, *, on_accepted=None, **media):
        sent.append((job_type, params, media))
        return Path(output_path), "job-9"

    monkeypatch.setattr(higgsfield, "generate", fake_generate)
    refs = [vg.ShotReference("image", "ref.png", "角色参考"), vg.ShotReference("audio", "a.wav", "音色")]
    logs = []
    result = asyncio.run(
        vg.create_video_generator("higgsfield:seedance_2_5").generate(
            "first.png", "p", str(tmp_path / "a.mp4"), aspect_ratio="9:16", duration=5,
            references=refs, audio_setting="off",
        )
    )
    asyncio.run(
        vg.create_video_generator("higgsfield:kling3_0?mode=pro").generate(
            "first.png", "p", str(tmp_path / "b.mp4"), references=refs, on_log=logs.append,
        )
    )

    assert result.status is vg.VideoGenStatus.DONE and result.task_id == "job-9"
    job_type, params, media = sent[0]
    assert job_type == "seedance_2_5"
    assert (params["mode"], params["generate_audio"], params["resolution"]) == ("omni_reference", False, "480p")
    assert (media["start_image"], media["refs"], media["audio_refs"]) == ("first.png", ["ref.png"], ["a.wav"])
    job_type, params, media = sent[1]
    assert (job_type, params["mode"], media["refs"], media["audio_refs"]) == ("kling3_0", "pro", [], [])
    assert logs == [
        "Higgsfield kling3_0?mode=pro: image_references ignorati",
        "Higgsfield kling3_0?mode=pro: audio_references ignorati",
    ]


def test_higgsfield_quote_prices_the_shaped_request(higgsfield_catalog, monkeypatch):
    import asyncio

    priced = []

    async def fake_cost(job_type, params, media):
        priced.append((job_type, params, media))
        return 3.0

    monkeypatch.setattr(higgsfield, "_cost", fake_cost)
    assert asyncio.run(higgsfield.quote("seedance_2_0?mode=fast", duration=5, resolution="1080p")) == 3.0
    job_type, params, media = priced[0]
    assert (job_type, params["mode"], params["resolution"], media) == ("seedance_2_0", "fast", "720p", [])


def test_retired_video_backends_map_to_the_default(monkeypatch):
    from novelvideo.generators.video_generator import normalize_video_backend, video_engine

    for retired in (None, "", "higgsfield", "newapi_seedance-2.0-fast", "huimeng_seedance-1.5-pro",
                    "comfyui", "ltx23", "grok_720", "seedance_2", "seedance_fast"):
        assert normalize_video_backend(retired) == DEFAULT_BACKEND
    assert normalize_video_backend("higgsfield:kling3_0?mode=pro") == "higgsfield:kling3_0?mode=pro"
    assert normalize_video_backend("H3.C") == "h3c"
    assert video_engine("higgsfield:kling3_0?mode=pro") == ("higgsfield", "kling3_0?mode=pro")
    monkeypatch.setenv("VIDEO_BACKEND", "h3c")
    assert normalize_video_backend("comfyui") == "h3c"


def test_h3_failure_is_reported_and_leaves_no_output(tmp_path, monkeypatch):
    import asyncio

    from novelvideo.generators.video_generator import H3VideoGenerator, VideoGenStatus

    fake = tmp_path / "h3"
    fake.write_text("#!/bin/sh\necho 'h3: prompt rejected' >&2\necho 'h3: wrote nothing' >&2\nexit 2\n")
    fake.chmod(0o755)
    monkeypatch.setenv("H3C_BINARY", str(fake))
    out = tmp_path / "o.mp4"
    result = asyncio.run(H3VideoGenerator().generate(None, "p", str(out), duration=1))

    assert result.status is VideoGenStatus.FAILED
    assert result.error == "h3.c rifiutato: h3: prompt rejected"
    assert not out.exists()


def test_h3_runs_from_its_own_dir_after_stopping_mtplx(tmp_path, monkeypatch):
    import asyncio

    from novelvideo.engines import mtplx

    bin_dir = tmp_path / "h3c"
    bin_dir.mkdir()
    fake = bin_dir / "h3"
    fake.write_text('#!/bin/sh\necho "h3: cwd $(pwd -P)" >&2\nexit 2\n')
    fake.chmod(0o755)
    monkeypatch.setenv("H3C_BINARY", str(fake))
    stopped = []
    monkeypatch.setattr(mtplx, "stop", lambda: stopped.append(True))

    with pytest.raises(EngineError) as err:
        asyncio.run(h3c.generate("p", tmp_path / "o.mp4", duration=0.5))

    assert str(err.value) == f"h3.c rifiutato: h3: cwd {bin_dir.resolve()}"
    assert stopped == [True]


def test_mtplx_stop_ends_only_the_server_this_process_started(monkeypatch):
    import subprocess

    from novelvideo.engines import mtplx

    mtplx.stop()  # nothing started: no-op
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    monkeypatch.setattr(mtplx, "_started", proc)
    mtplx.stop()

    assert proc.poll() is not None and mtplx._started is None


@pytest.fixture
def owned_mtplx(monkeypatch):
    """An MTPLX 'started by this process' whose stops are counted, not executed."""
    from novelvideo.engines import mtplx

    stops = []

    def fake_stop_locked():
        stops.append(mtplx._started)
        mtplx._started = None

    monkeypatch.setattr(mtplx, "_started", object())
    monkeypatch.setattr(mtplx, "_leases", 0)
    monkeypatch.setattr(mtplx, "_idle_timer", None)
    monkeypatch.setattr(mtplx, "_stop_locked", fake_stop_locked)
    yield mtplx, stops
    with mtplx._lock:
        mtplx._cancel_idle_locked()


def _settle(mtplx, seconds=0.3):
    time.sleep(seconds)
    timer = mtplx._idle_timer
    if timer is not None:
        timer.join(1)


def test_mtplx_lease_keeps_server_then_idle_stops_it_once(owned_mtplx, monkeypatch):
    mtplx, stops = owned_mtplx
    monkeypatch.setenv("MTPLX_IDLE_SECONDS", "0.1")
    with mtplx.lease():
        with mtplx.lease():
            pass
        _settle(mtplx)
        assert stops == []  # the outer lease is still held
    _settle(mtplx)
    assert len(stops) == 1 and mtplx._started is None


def test_mtplx_new_lease_during_grace_cancels_the_stop(owned_mtplx, monkeypatch):
    mtplx, stops = owned_mtplx
    monkeypatch.setenv("MTPLX_IDLE_SECONDS", "0.3")
    with mtplx.lease():
        pass
    with mtplx.lease():
        time.sleep(0.5)
        assert stops == []
    monkeypatch.setenv("MTPLX_IDLE_SECONDS", "0")
    with mtplx.lease():
        pass
    _settle(mtplx)
    assert len(stops) == 1


def test_mtplx_idle_zero_stops_right_away(owned_mtplx, monkeypatch):
    mtplx, stops = owned_mtplx
    monkeypatch.setenv("MTPLX_IDLE_SECONDS", "0")
    with mtplx.lease():
        pass
    _settle(mtplx, 0.05)
    assert len(stops) == 1


def test_mtplx_idle_leaves_an_external_server_alone(owned_mtplx, monkeypatch):
    mtplx, stops = owned_mtplx
    monkeypatch.setattr(mtplx, "_started", None)  # served by someone else
    monkeypatch.setenv("MTPLX_IDLE_SECONDS", "0")
    with mtplx.lease():
        pass
    _settle(mtplx)
    assert stops == [] and mtplx._idle_timer is None


def test_text_model_request_holds_an_mtplx_lease(monkeypatch):
    import asyncio

    from pydantic_ai.models.openai import OpenAIChatModel

    from novelvideo import config
    from novelvideo.engines import mtplx

    seen = []

    async def fake_request(self, *args, **kwargs):
        seen.append(mtplx._leases)
        return "ok"

    monkeypatch.setattr(OpenAIChatModel, "request", fake_request)
    model = config.get_text_pydantic_model("MODEL_NAME", "unused")
    assert asyncio.run(model.request([], None, None)) == "ok"
    assert seen == [1] and mtplx._leases == 0


def test_media_catalog_video_entries_come_from_model_schemas(higgsfield_catalog):
    from novelvideo.media_catalog import media_model_catalog

    entries = {entry["catalogId"]: entry for entry in media_model_catalog("video")}

    assert "h3c" not in entries  # binary not installed
    default = entries[DEFAULT_BACKEND]
    assert default["provider"] == "higgsfield" and default["label"] == "Seedance 2.0 Fast"
    assert "all_reference" in default["supportedModes"]
    assert (default["referenceImageMax"], default["referenceAudioMax"]) == (9, 3)
    assert (default["minDuration"], default["maxDuration"]) == (4, 15)
    seedance25 = entries["higgsfield:seedance_2_5"]
    assert {"video_edit", "video_extend"} <= set(seedance25["supportedModes"])
    assert seedance25["referenceImageMax"] == 30
    kling = entries["higgsfield:kling3_0?mode=pro"]
    assert kling["supportedModes"] == ["text_to_video", "first_frame", "image_to_video", "first_last_frame"]
    assert kling["referenceImageMax"] == 0


def test_higgsfield_reattached_job_is_reported_without_paying_again(tmp_path, monkeypatch):
    import asyncio

    out = tmp_path / "a.mp4"
    (tmp_path / "a.mp4.higgsfield-job").write_text("job-7")
    calls, accepted = [], []

    async def fake_json(args, **_):
        calls.append(args[:2])
        return {"status": "failed"}

    monkeypatch.setattr(higgsfield, "_json", fake_json)
    with pytest.raises(EngineError, match="job-7"):
        asyncio.run(higgsfield.generate("seedance_2_0", {}, out, on_accepted=lambda *a: accepted.append(a)))

    assert accepted == [("job-7", None)]
    assert calls == [["generate", "get"]]  # no cost, no create
