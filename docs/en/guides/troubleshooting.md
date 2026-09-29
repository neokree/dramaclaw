<!-- lang-switch -->
**English** · [简体中文](../../zh/guides/troubleshooting.md)

# Troubleshooting

> Common failures and how to diagnose them when self-hosting DramaClaw CE. Check the logs first: `docker compose logs -f api`, or the terminal output of `novelvideo api` during local development.

## Startup

| Symptom | Diagnosis |
|---|---|
| **Container won't start / exits immediately** | Check `docker compose logs api` and follow the startup error to the configuration, port, or data-volume problem. |
| **Port `8780` already in use** | Change the left-hand value of `ports` in compose, e.g. `8888:8780`; or stop the process holding it (`lsof -i :8780`). |
| **Health check stays unhealthy** | The probe hits `/api/v1/config`; if the API itself errors, check the startup logs to pinpoint the real exception. |
| **Local dev won't start: Python version** | Requires **3.11–3.12** (`>=3.11,<3.13`). Run `uv python pin 3.12` or install the matching version, then `uv sync`. |

## Models / engines

| Symptom | Diagnosis |
|---|---|
| **Every model call errors** | Open **Settings → Engines** and check that the engines you selected in `.env` show “Available”. See [Configuring models](../getting-started/configuring-models.md#troubleshooting). |
| **Higgsfield unavailable or `higgsfield auth login` error** | Install the Higgsfield CLI (or set `HIGGSFIELD_BINARY`) and run `higgsfield auth login`; `higgsfield account status` shows the credit balance. |
| **Draw Things unavailable** | Enable **Settings → API Server** (HTTP, port 7860) in Draw Things, or fix `DRAWTHINGS_URL`. |
| **Text fails with MTPLX** | `MTPLX_BINARY` is missing, or another server at `MTPLX_BASE_URL` serves a model other than `MTPLX_MODEL`. |
| **Structured steps fail with `Exceeded maximum output retries`** (character extraction, script planning…) while plain text works | The text model did not return a function/tool call. The task log (v2.0.3+) shows the retry prompt and cause. Under OpenRouter, pick a model that supports tool calling in `OPENROUTER_MODEL` or the per-feature `*_MODEL` override. |
| **Text model times out** | Increase `TEXT_TIMEOUT_SECONDS` (default 300); if a system proxy intercepts a local engine, set `TEXT_TRUST_ENV=false`. |

## Media / ffmpeg

| Symptom | Diagnosis |
|---|---|
| **Compositing stage reports ffmpeg not found** | Local development requires installing ffmpeg yourself (Docker bundles it); or point to a path with `FFMPEG_PATH`. See the [ffmpeg guide](ffmpeg.md). |
| **Compositing fails with encoder unavailable** | The default codec is `libx264` (H.264), which your ffmpeg build must include; or change `VIDEO_CODEC`. |
| **Output is black / duration is wrong** | Usually upstream image/audio artifacts are missing; review the logs of preceding stages to confirm the assets were generated. |

## Data / upgrades

| Symptom | Diagnosis |
|---|---|
| **Data gone after a rebuild** | Data lives in the named volume `ce-data` (`/data` inside the container). `docker compose down` keeps the volume—**do not add `-v`** (it deletes the volume). For backups see the [self-hosting handbook](self-hosting.md#5-where-the-data-lives--backups). |
| **Config error after an upgrade** | Source build (`docker-compose.yml`): `git pull && docker compose up -d --build`. Prebuilt images (`docker-compose.release.yml`): `docker compose -f docker-compose.release.yml pull && docker compose -f docker-compose.release.yml up -d` (bump `DRAMACLAW_VERSION` in `.env` if you pin it). See the self-hosting guide §6. |

## world features (3DGS/SHARP)

| Symptom | Diagnosis |
|---|---|
| **`FileNotFoundError` pointing at `BuilderGPT/...`** | These heavyweight feature scripts are not in the slim CE package; the plain text→video pipeline doesn't need them. They're only required for the 3D/voxel pipeline. |
| **`uv sync --extra world` install fails** | Use uv (not pip) so the dependency overrides take effect; GPU acceleration needs a CUDA environment, while slim/CPU environments only support the CPU path. |

## Still stuck?

- Usage / ideas → [GitHub Discussions](https://github.com/dramaclaw/dramaclaw/discussions)
- Confirmed a bug → [File a bug](https://github.com/dramaclaw/dramaclaw/issues/new?template=bug_report.yml) (attach logs, reproduction steps, environment)
- Security issue → do not use a public issue; see [SECURITY](../../../SECURITY.md)

## Related

- [Installation guide](../getting-started/installation.md) ｜ [Quickstart](../getting-started/quickstart.md) ｜ [Self-hosting handbook](self-hosting.md)
