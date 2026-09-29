from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_c1_eg21_release_feed_drops_untrusted_release_url(tmp_path, monkeypatch):
    from novelvideo.ports.local.release_feed import LocalReleaseFeed

    monkeypatch.setenv("RELEASE_NOTIFICATIONS_ENABLED", "true")
    notes = tmp_path / "release-notes.md"
    notes.write_text(
        "# v1.0.0\n## User-facing Highlights (en)\n- **Current**: local\n",
        encoding="utf-8",
    )

    async def fetcher():
        return {
            "tag_name": "v2.0.0",
            "html_url": "https://attacker.example/secret-object-canary",
            "body": "# v2.0.0\n## User-facing Highlights (en)\n- **New**: item\n",
        }

    feed = await LocalReleaseFeed(
        notes_path=notes,
        version_reader=lambda: "1.0.0",
        github_fetcher=fetcher,
    ).current(locale="en")

    assert feed.update_available is True
    assert feed.release_url is None

