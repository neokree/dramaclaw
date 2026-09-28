"""Subprocess helper shared by the CLI engines."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Sequence

from novelvideo.task_backend.subprocesses import run_project_subprocess


class EngineError(RuntimeError):
    """An engine refused or failed; the message is safe to show to the user."""


async def run(
    args: Sequence[str], *, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run a CLI off the event loop, in its own process group, killed on task cancel."""
    return await asyncio.to_thread(
        run_project_subprocess,
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def error_text(proc: subprocess.CompletedProcess) -> str:
    text = f"{proc.stderr or ''}{proc.stdout or ''}".strip()
    return text.removeprefix("Error: ").strip()
