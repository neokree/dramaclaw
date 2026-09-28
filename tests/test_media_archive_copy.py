from __future__ import annotations

from pathlib import Path

import pytest

from novelvideo import ports
from novelvideo.media_archive_copy import copy_archived_result
from novelvideo.ports.registry import PortNotRegistered
from novelvideo.ports.video_delivery import VideoDeliveryError


ARCHIVE = {
    "asset_id": 42,
    "status": "success",
    "storage_provider": "aliyun_oss",
    "bucket": "archive-source",
    "object_key": "results/image.png",
    "url": "https://archive.example/image.png",
    "content_type": "image/png",
    "size": 5,
    "sha256": "a" * 64,
}


@pytest.mark.asyncio
async def test_disabled_media_copy_keeps_download_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ports, "get_video_result_delivery", lambda: None)
    assert not await copy_archived_result(None, tmp_path / "image.png")


@pytest.mark.asyncio
async def test_enabled_media_copy_uses_archive_once(monkeypatch, tmp_path: Path):
    calls = []

    class Delivery:
        async def deliver(self, *, source, output_path):
            calls.append((source, output_path))
            Path(output_path).write_bytes(b"image")

    monkeypatch.setattr(ports, "get_video_result_delivery", lambda: Delivery())
    output = tmp_path / "nested" / "image.png"
    assert await copy_archived_result(ARCHIVE, output)
    assert output.read_bytes() == b"image"
    assert len(calls) == 1
    assert calls[0][0].object_key == "results/image.png"


@pytest.mark.asyncio
async def test_media_copy_preserves_existing_image_before_replacement(
    monkeypatch, tmp_path: Path
):
    class Delivery:
        async def deliver(self, *, source, output_path):
            Path(output_path).write_bytes(b"new-image")

    monkeypatch.setattr(ports, "get_video_result_delivery", lambda: Delivery())
    output = tmp_path / "scene.png"
    previous = tmp_path / "scene.previous.png"
    output.write_bytes(b"old-image")
    assert await copy_archived_result(
        ARCHIVE,
        output,
        before_copy=lambda: output.replace(previous),
    )
    assert previous.read_bytes() == b"old-image"
    assert output.read_bytes() == b"new-image"


@pytest.mark.asyncio
async def test_enabled_media_copy_requires_complete_archive(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(ports, "get_video_result_delivery", lambda: object())
    with pytest.raises(VideoDeliveryError, match="MEDIA_ARCHIVE_UNAVAILABLE"):
        await copy_archived_result({"status": "pending"}, tmp_path / "image.png")


@pytest.mark.asyncio
async def test_enabled_copy_does_not_hide_missing_delivery_port(
    monkeypatch, tmp_path: Path
):
    def missing_port():
        raise PortNotRegistered("video_result_delivery")

    monkeypatch.setenv("ST_MEDIA_ARCHIVE_COPY_ENABLED", "true")
    monkeypatch.setattr(ports, "get_video_result_delivery", missing_port)
    with pytest.raises(VideoDeliveryError, match="MEDIA_DELIVERY_PORT_UNAVAILABLE"):
        await copy_archived_result(ARCHIVE, tmp_path / "image.png")

@pytest.mark.asyncio
async def test_audio_url_copies_archive_without_downloading(
    monkeypatch, tmp_path: Path
):
    import httpx

    pytest.importorskip("langid")

    from novelvideo import config
    from novelvideo.freezone.audio_node import _write_newapi_audio_speech

    class Delivery:
        async def deliver(self, *, source, output_path):
            Path(output_path).write_bytes(b"copied-audio")

    class Response:
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            pass

        def json(self):
            return {"audio": {"url": ARCHIVE["url"]}, "archive": ARCHIVE}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return Response()

        async def get(self, *args, **kwargs):
            raise AssertionError("archive copy must not download the URL")

    monkeypatch.setattr(ports, "get_video_result_delivery", lambda: Delivery())
    monkeypatch.setattr(
        config,
        "get_newapi_runtime_credentials",
        lambda **_: ("token", "https://gateway.example/v1"),
    )
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    output = tmp_path / "audio.mp3"
    await _write_newapi_audio_speech(
        output_path=output,
        model="test-audio-model",
        input_text="hello",
        response_format="mp3",
    )
    assert output.read_bytes() == b"copied-audio"
