
import pytest
from pydantic import ValidationError


def test_video_enhance_target_fps_accepts_common_fractional_rates() -> None:
    from novelvideo.api.schemas import FreezoneVideoUpscaleRequest

    for fps in (23.976, 29.97, 59.94, 119.88):
        body = FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", target_fps=fps)
        assert body.target_fps == fps
    for fps in (0, 120.001, 23.9765):
        with pytest.raises(ValidationError):
            FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", target_fps=fps)


def test_video_enhance_accepts_slowdown_through_five_times() -> None:
    from novelvideo.api.schemas import FreezoneVideoUpscaleRequest

    for slowdown in ("auto", "2x", "3x", "4x", "5x"):
        body = FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", slowdown=slowdown)
        assert body.slowdown == slowdown
    with pytest.raises(ValidationError):
        FreezoneVideoUpscaleRequest(source_url="/static/source.mp4", slowdown="6x")


def test_video_enhance_quote_carries_trusted_frame_rate_inputs() -> None:
    from novelvideo.api.routes.freezone import _video_enhance_billing
    from novelvideo.api.schemas import FreezoneVideoUpscaleRequest

    body = FreezoneVideoUpscaleRequest(
        source_url="/static/source.mp4", resolution="1080p", slowdown="2x",
    )
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["feature_key"] == "freezone.video_enhance"
    assert billing["pricing_stages"][0]["duration_seconds"] == 11
    assert billing["pricing_stages"][1] == {
        "mode": "video_frame_rate", "catalog_id": "frame-id",
        "resolution": "1080p", "duration_seconds": 21,
        "source_fps": 23.976, "target_fps": 23.976, "slowdown": "2x",
        "smart_interpolation": True,
    }

    body.smart_interpolation = False
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["pricing_stages"][1]["smart_interpolation"] is False

    body.target_fps = 59.94
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["pricing_stages"][1]["target_fps"] == 59.94

    body.slowdown = "5x"
    billing = _video_enhance_billing(
        body, {"duration": 10.2, "fps": 23.976},
        {"catalog_id": "upscale-id"}, {"catalog_id": "frame-id"},
    )
    assert billing["pricing_stages"][1]["duration_seconds"] == 51
    assert billing["pricing_stages"][1]["slowdown"] == "5x"
