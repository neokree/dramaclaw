<!-- lang-switch -->
**English** · [简体中文](../../zh/getting-started/configuring-models.md)

# Configuring Models

DramaClaw CE generates text, images, video, and audio through a small set of engines. Local engines run on your machine and cost nothing; Higgsfield runs in the cloud and bills its own credits. Engines are chosen with environment variables in `.env` (see [`.env.example`](../../../.env.example)); there is no per-model mapping to maintain.

| Stage | Engine | Selection |
|---|---|---|
| Text / vision | MTPLX (local, default) or OpenRouter | `TEXT_ENGINE=mtplx` / `TEXT_ENGINE=openrouter` |
| Images | Draw Things (local), any Higgsfield image model, or OpenRouter image models | `drawthings` / `higgsfield:<model>` / `openrouter:<model>` |
| Video | any Higgsfield video model, or h3.c / MiniMax H3 (local) | `higgsfield:<model>` / `h3c` |
| Voices, music, sound effects | Higgsfield | `HIGGSFIELD_TTS_*` |

After startup, open `http://localhost:8080` and click **Settings**. The **Engines** section shows, for each engine, whether it is reachable, the remaining Higgsfield credits, whether MTPLX is running or starts on demand, and the active text engine. It reads `GET /api/v1/model-gateway/engines` and is read-only: change engines in `.env` and restart the API.

## Text: MTPLX or OpenRouter

`TEXT_ENGINE` picks the engine for every text call and for text-with-image (vision) calls. Structured extraction (characters, scenes, props), planning metadata and Hermes chat follow it.

### MTPLX (default)

MTPLX is a local OpenAI-compatible server (`mtplx serve`). DramaClaw starts it on the first text request if nothing answers at `MTPLX_BASE_URL`, and needs no key. The default model has a vision tower and accepts `image_url` input.

| Variable | Default |
|---|---|
| `MTPLX_BASE_URL` | `http://127.0.0.1:8000/v1` |
| `MTPLX_MODEL` | `hawhyhb-qwen36-35b-a3b-uncensored-heretic-mtplx-4bit-fp16` |
| `MTPLX_BINARY` | `~/.mtplx/bin/mtplx` |
| `MTPLX_MODEL_PATH` | `~/.mtplx/models/hawhyhb--Qwen3.6-35B-A3B-Uncensored-Heretic-MTPLX-4bit-FP16` |
| `MTPLX_IDLE_SECONDS` | `120` (seconds a server DramaClaw started stays up after the last text request or chat turn; `0` stops it at once) |

If a server already answers at `MTPLX_BASE_URL` but does not serve `MTPLX_MODEL`, text calls fail instead of starting a second server. MTPLX serves one model, so the per-feature `*_MODEL` overrides in `.env.example` are ignored.

### OpenRouter

Set `TEXT_ENGINE=openrouter` and `OPENROUTER_API_KEY`. `OPENROUTER_MODEL` sets the default model (`google/gemma-4-26b-a4b-it` when empty). Per-feature overrides such as `CHARACTER_BUILD_MODEL` or `FREEZONE_VISION_MODEL` apply only under OpenRouter and take the `vendor/model` form; older `DC-*` logical names are ignored.

`TEXT_TIMEOUT_SECONDS` (default 300) sets the text HTTP timeout. `TEXT_TRUST_ENV` controls whether the client reads the system proxy; it defaults to `false` for MTPLX and `true` for OpenRouter.

## Higgsfield (video, images, audio)

Higgsfield is driven through its CLI. Install it, then sign in once:

```bash
higgsfield auth login
higgsfield account status   # shows the credit balance
```

DramaClaw looks for the CLI in `HIGGSFIELD_BINARY`, then on `PATH`, then at `/opt/homebrew/bin/higgsfield`. Models are not hard-coded: the catalog comes from `higgsfield model list`, and each request is shaped by the model's schema (`higgsfield model get <job> --json`), so a parameter the model does not accept is never sent. Catalog and schemas are cached on disk for a day in `HIGGSFIELD_CACHE_DIR` (default `~/.cache/dramaclaw/higgsfield`).

A Higgsfield selection is `higgsfield:<model_ref>`. The reference can carry a preset as a query string, for example `higgsfield:kling3_0?mode=pro` or `higgsfield:gpt_image_2_5?variant=flare`; `mode` and `variant` mean different things per model. Featured models (Seedance, Kling 3.0, GPT Image, Nano Banana, Seedream) are listed first in the pickers; every other model on the platform works through the same schema-driven path.

A job id is written to disk before DramaClaw waits for the result, so an interrupted paid job is reattached on retry instead of being paid twice. The CLI has no cancel.

### Video

| Variable | Default | Notes |
|---|---|---|
| `VIDEO_BACKEND` | Empty | `higgsfield:<model>` or `h3c`. Empty falls back to `HIGGSFIELD_VIDEO_MODEL`. |
| `HIGGSFIELD_VIDEO_MODEL` | `seedance_2_0?mode=fast` | Default video model, i.e. backend `higgsfield:seedance_2_0?mode=fast`. |
| `HIGGSFIELD_VIDEO_MODE` | `fast` | Seedance 2.0 `fast` / `std`; `fast` outputs 480p/720p only. |
| `HIGGSFIELD_VIDEO_RESOLUTION` | `480p` | Default resolution. |

Stored selections from retired providers resolve to `VIDEO_BACKEND`, then to `HIGGSFIELD_VIDEO_MODEL`.

### Images

| Variable | Default |
|---|---|
| `DEFAULT_IMAGE_SELECTION` | `higgsfield:nano_banana_flash` |
| `DEFAULT_CHARACTER_IMAGE_SELECTION` / `DEFAULT_SKETCH_IMAGE_SELECTION` / `DEFAULT_RENDER_IMAGE_SELECTION` | `DEFAULT_IMAGE_SELECTION` |
| `DIRECTOR_SKETCH_IMAGE_SELECTION` / `DIRECTOR_SKETCH_IMAGE_QUALITY` | `higgsfield:gpt_image_2` / `low` |

Each variable takes `drawthings`, `higgsfield:<model>`, or `openrouter:<model>`. Aspect ratio, resolution, and quality are mapped onto the values the chosen model's schema accepts.

### Voices, music, and sound effects

- A character or narrator voice built from an uploaded reference sample goes to `seed_audio`, with the sample as the audio reference.
- A Higgsfield voice (`higgsfield voices list`, preset or cloned) goes to `HIGGSFIELD_TTS_MODEL` (default `text2speech_v2`) with `HIGGSFIELD_TTS_VARIANT` (default `elevenlabs`). `HIGGSFIELD_TTS_VOICE` sets the default voice id; when empty, the first preset voice is used.
- Music uses `sonilo_music`; sound effects use `mirelo_text_to_audio`.

## Draw Things (local images)

Draw Things exposes an A1111-compatible HTTP API. In Draw Things, enable **Settings → API Server** (HTTP, port 7860), then select `drawthings`, for example `DEFAULT_IMAGE_SELECTION=drawthings`.

| Variable | Default | Notes |
|---|---|---|
| `DRAWTHINGS_URL` | `http://127.0.0.1:7860` | API server address. |
| `DRAWTHINGS_MODEL` | Empty | Empty uses the model currently loaded in Draw Things. |
| `DRAWTHINGS_STEPS` | Empty | Empty uses the current Draw Things setting. |

The first reference image is used as the img2img init image.

## OpenRouter images

Set `OPENROUTER_API_KEY` and select `openrouter:<model>`, for example `openrouter:google/gemini-3.1-flash-image-preview`. `OPENROUTER_IMAGE_MODELS` is a comma-separated list of the models offered in the pickers; when empty it is `google/gemini-3.1-flash-image-preview,openai/gpt-5.4-image-2`. Reference images are sent inline as data URLs.

## h3.c (local video)

h3.c runs MiniMax H3 locally on Metal. It appears as the `h3c` backend only when both the binary and the weights are present:

| Variable | Default |
|---|---|
| `H3C_BINARY` | `~/Developer/AI-Tools/h3.c/h3` |
| `H3C_WEIGHTS` | `~/Developer/AI-Tools/h3.c/MiniMax-H3` |

h3.c renders at 24 fps, accepts start and end frames, supports the ratios `9:16`, `16:9`, `1:1`, `4:3`, `3:4`, and `4:5`, and does not take image, video, or audio references.

## Credits and usage

Before a Higgsfield job runs, DramaClaw quotes its credit price. Every job is recorded in the project's usage ledgers with the credits Higgsfield charged for it; local engines (Draw Things, h3.c) cost 0, and OpenRouter records the USD cost its response reports (0 when none). Read them per project:

- `GET /api/v1/projects/{project}/video-usage`: video jobs and credits spent, plus the Higgsfield balance.
- `GET /api/v1/projects/{project}/character-image-usage` and `GET /api/v1/projects/{project}/episodes/{episode}/sketch-image-usage`: image requests.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Higgsfield shows “Unavailable” | The CLI is not installed or not found (`HIGGSFIELD_BINARY`), or the session expired: run `higgsfield auth login`. |
| Draw Things shows “Unavailable” | Enable **Settings → API Server** (HTTP, port 7860) in Draw Things, or fix `DRAWTHINGS_URL`. |
| h3.c does not appear among video models | `H3C_BINARY` or `H3C_WEIGHTS` does not exist. |
| Text calls fail with MTPLX | `MTPLX_BINARY` is missing, or another server at `MTPLX_BASE_URL` serves a different model than `MTPLX_MODEL`. |
| OpenRouter shows “Unavailable” | `OPENROUTER_API_KEY` is not set. |
| `127.0.0.1` engines unreachable from Docker | Inside a container `127.0.0.1` is the container itself. Point `DRAWTHINGS_URL` / `MTPLX_BASE_URL` at an address of the host reachable from the backend. The Higgsfield CLI and h3.c are not in the image. |

## Related files

- `src/novelvideo/engines/`: engine drivers (Higgsfield, Draw Things, OpenRouter images, h3.c, MTPLX, audio).
- `src/novelvideo/media_catalog.py`: the media model catalog built from the installed engines.
- `.env.example`: environment variable reference.
- `docker-compose.yml` (source build) / `docker-compose.release.yml` (images): the deployment files (api + web).
- [Self-Hosting Handbook](../guides/self-hosting.md)
- [Environment Variable Reference](../reference/environment-variables.md)
