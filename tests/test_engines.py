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


def test_higgsfield_video_params_clamp_and_refuse():
    assert higgsfield.video_params("p", "9:16", 2.2)["duration"] == 4
    assert higgsfield.video_params("p", "9:16", 20)["duration"] == 15
    with pytest.raises(EngineError):
        higgsfield.video_params("p", "4:5", 5)


def test_h3_canvas_frames_seed():
    assert h3c.canvas("9:16") == (576, 1024)
    assert h3c.canvas("16:9") == (1024, 576)
    assert h3c.canvas("4:5") == (896, 1120)
    assert [h3c.frames(s) for s in (0.1, 1, 4)] == [5, 39, 107]
    assert h3c.seed_of("block.v1") == h3c.seed_of("block.v1") != h3c.seed_of("block.v2")


def test_drawthings_size_is_multiple_of_64():
    w, h = drawthings.size_for("9:16")
    assert w % 64 == 0 and h % 64 == 0 and w < h
    assert drawthings.size_for("1:1") == (1024, 1024)
