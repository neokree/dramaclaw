from __future__ import annotations

import pytest

pytestmark = pytest.mark.m09


def test_ce_does_not_expose_archive_delivery_port(monkeypatch) -> None:
    import novelvideo.ports as ports

    monkeypatch.setattr(ports.runtime_env, "edition", lambda: "ce")

    def forbidden(_name: str):
        raise AssertionError("CE must not resolve the EE archive delivery port")

    monkeypatch.setattr(ports, "get_port", forbidden)

    assert ports.get_video_result_delivery() is None


def test_ee_resolves_archive_delivery_port(monkeypatch) -> None:
    import novelvideo.ports as ports

    delivery = object()
    monkeypatch.setattr(ports.runtime_env, "edition", lambda: "ee")
    monkeypatch.setattr(
        ports,
        "get_port",
        lambda name: delivery if name == "video_result_delivery" else None,
    )

    assert ports.get_video_result_delivery() is delivery
