"""MTPLX: local OpenAI-compatible text server, started on demand.

Port of MacGen's `mtplx_text_engine.rs`, reduced: a server already serving the
model is used as is; otherwise `mtplx serve` is started with
MTPLX_APP_PARENT_PID so it stops by itself when this backend exits.
`stop()` shuts down a server this process started (h3.c calls it to free
~30 GB); the next text request starts it again.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx

from novelvideo.engines._proc import EngineError

START_TIMEOUT_SECONDS = 120
_lock = threading.Lock()
_started: subprocess.Popen | None = None


def base_url() -> str:
    return os.environ.get("MTPLX_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")


def model_id() -> str:
    return os.environ.get(
        "MTPLX_MODEL", "hawhyhb-qwen36-35b-a3b-uncensored-heretic-mtplx-4bit-fp16"
    )


def binary() -> str:
    return os.environ.get("MTPLX_BINARY", str(Path.home() / ".mtplx/bin/mtplx"))


def model_path() -> str:
    return os.environ.get(
        "MTPLX_MODEL_PATH",
        str(Path.home() / ".mtplx/models/hawhyhb--Qwen3.6-35B-A3B-Uncensored-Heretic-MTPLX-4bit-FP16"),
    )


def served_models() -> list[str] | None:
    """Model ids the server lists, or None when nothing answers."""
    try:
        resp = httpx.get(f"{base_url()}/models", timeout=2, trust_env=False)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    return [str(m.get("id")) for m in (resp.json() or {}).get("data") or []]


def status() -> dict[str, object]:
    models = served_models()
    if models is not None:
        ok = model_id() in models
        return {"available": ok, "running": True,
                "reason": "" if ok else f"MTPLX serve un altro modello: {models}"}
    if not Path(binary()).exists():
        return {"available": False, "running": False, "reason": f"mtplx non trovato in {binary()}"}
    if not Path(model_path()).exists():
        return {"available": False, "running": False, "reason": f"modello MTPLX assente: {model_path()}"}
    return {"available": True, "running": False, "reason": ""}


def ensure_running() -> str:
    """Return the base URL of a server serving `model_id()`, starting one if needed."""
    global _started
    with _lock:
        models = served_models()
        if models is not None:
            if model_id() not in models:
                raise EngineError(f"MTPLX su {base_url()} serve {models}, non {model_id()}.")
            return base_url()
        if not Path(binary()).exists():
            raise EngineError(f"MTPLX non installato ({binary()}).")
        host_port = base_url().split("://", 1)[-1].split("/", 1)[0]
        host, _, port = host_port.partition(":")
        proc = subprocess.Popen(
            [binary(), "serve", "--host", host, "--port", port or "8000",
             "--model", model_path(), "--model-id", model_id(),
             "--profile", "sustained", "--context-window", "131072",
             "--reasoning", "auto", "--reasoning-effort", "medium",
             "--unsafe-force-unverified", "--yes", "--no-stats-footer"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "MTPLX_APP_PARENT_PID": str(os.getpid())},
            start_new_session=True,
        )
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise EngineError(f"mtplx serve è uscito con codice {proc.returncode}.")
            if model_id() in (served_models() or []):
                _started = proc
                return base_url()
            time.sleep(0.25)
        proc.terminate()
        raise EngineError(f"MTPLX non pronto dopo {START_TIMEOUT_SECONDS}s.")


def stop() -> None:
    """Stop the server this process started, if any; a server started elsewhere is left alone."""
    global _started
    with _lock:
        proc, _started = _started, None
        if proc is None or proc.poll() is not None:
            return
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
