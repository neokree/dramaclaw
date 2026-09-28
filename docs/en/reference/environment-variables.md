<!-- lang-switch -->
**English** · [简体中文](../../zh/reference/environment-variables.md)

# Environment Variables Reference

> Configuration is injected through a `.env` file in the project root (copied from `.env.example`). **`.env.example` is the authoritative, complete set with per-item comments**; this page groups the most commonly used variables and their defaults by category for quick reference.

## Runtime Environment and Paths

| Variable | Default | Description |
|---|---|---|
| `ST_EDITION` | `ce` (forced by compose/image) | Edition identifier. CE mode cannot be downgraded. |
| `NOVELVIDEO_DATA_ROOT` | `.` (set to `/data` under Docker) | Data root directory; the three items below derive from it by default. |
| `NOVELVIDEO_OUTPUT_DIR` | `$DATA_ROOT/output` | Output directory for finished videos and artifacts. Docker Compose pins it to `/data/output`, which is persisted in the `ce-data` volume. |
| `NOVELVIDEO_STATE_DIR` | `$DATA_ROOT/state` | Local state. |
| `NOVELVIDEO_RUNTIME_DIR` | `$DATA_ROOT/runtime` | Runtime temporary directory. |
| `ST_CONTROL_PLANE_DSN` / `ST_REDIS_URL` / `ST_CELERY_BROKER_URL` / `ST_CELERY_RESULT_BACKEND` | Empty (forced empty in CE) | Used only by EE/distributed; CE runs tasks inline in-process, so leave empty. |

## Generation Engines

Engines are chosen here, not in the web UI; **Settings → Engines** only shows their status. See [Configuring Models](../getting-started/configuring-models.md).

| Variable | Default | Description |
|---|---|---|
| `TEXT_ENGINE` | `mtplx` | Text and vision engine: `mtplx` (local) or `openrouter`. |
| `MTPLX_BASE_URL` / `MTPLX_MODEL` | `http://127.0.0.1:8000/v1` / see `.env.example` | Local MTPLX server, started on demand. `MTPLX_BINARY` and `MTPLX_MODEL_PATH` locate the binary and weights. |
| `OPENROUTER_API_KEY` | Empty | Key for OpenRouter text (`TEXT_ENGINE=openrouter`) and OpenRouter images. |
| `OPENROUTER_MODEL` | `google/gemma-4-26b-a4b-it` | Default OpenRouter text model. Per-feature `*_MODEL` overrides (`vendor/model`) apply only under OpenRouter. |
| `OPENROUTER_IMAGE_MODELS` | `google/gemini-3.1-flash-image-preview,openai/gpt-5.4-image-2` | OpenRouter image models offered as `openrouter:<model>`. |
| `TEXT_TIMEOUT_SECONDS` | `300` | HTTP timeout for text models (seconds). |
| `TEXT_TRUST_ENV` | `false` for MTPLX, `true` for OpenRouter | Whether the text client reads the system proxy. |
| `DEFAULT_IMAGE_SELECTION` | `higgsfield:nano_banana_flash` | Default image model: `drawthings`, `higgsfield:<model>`, or `openrouter:<model>`. `DEFAULT_CHARACTER_IMAGE_SELECTION`, `DEFAULT_SKETCH_IMAGE_SELECTION`, and `DEFAULT_RENDER_IMAGE_SELECTION` override it per stage. |
| `VIDEO_BACKEND` | Empty | Default video backend: `higgsfield:<model>` (e.g. `higgsfield:kling3_0?mode=pro`) or `h3c`. Empty uses `HIGGSFIELD_VIDEO_MODEL`. |
| `HIGGSFIELD_VIDEO_MODEL` / `HIGGSFIELD_VIDEO_MODE` / `HIGGSFIELD_VIDEO_RESOLUTION` | `seedance_2_0?mode=fast` / `fast` / `480p` | Default Higgsfield video model and parameters. |
| `HIGGSFIELD_BINARY` | `PATH`, then `/opt/homebrew/bin/higgsfield` | Higgsfield CLI; sign in first with `higgsfield auth login`. |
| `HIGGSFIELD_CACHE_DIR` | `~/.cache/dramaclaw/higgsfield` | Model catalog and schema cache (one day). |
| `HIGGSFIELD_TTS_MODEL` / `HIGGSFIELD_TTS_VARIANT` / `HIGGSFIELD_TTS_VOICE` | `text2speech_v2` / `elevenlabs` / first preset voice | Speech in a Higgsfield voice. Reference-sample voices use `seed_audio`. |
| `DRAWTHINGS_URL` | `http://127.0.0.1:7860` | Draw Things API server. `DRAWTHINGS_MODEL` and `DRAWTHINGS_STEPS` default to the current Draw Things settings. |
| `H3C_BINARY` / `H3C_WEIGHTS` | `~/Developer/AI-Tools/h3.c/h3` / `~/Developer/AI-Tools/h3.c/MiniMax-H3` | Local h3.c binary and MiniMax H3 weights. |

## Embedding Gateway

The bundled NewAPI now serves only the Cognee embedding model (`DC-cognee-embedding`). Its channel, address, and token are written to the local `settings.db` through the `/api/v1/model-gateway` API, not through environment variables.

| Variable | Default | Description |
|---|---|---|
| `NEWAPI_PROVISIONER_ENABLED` | `true` | Enables one-click initialization of the bundled NewAPI. |
| `NEWAPI_ADMIN_BASE_URL` | `http://127.0.0.1:3000` (set in `.env.example`) | NewAPI management address, without `/v1`. |
| `COGNEE_EMBEDDING_MODEL` / `COGNEE_EMBEDDING_DIM` | `DC-cognee-embedding` / `1024` | Default embedding model and dimensions; each project stores its own in `project_config.json`. |
| `EMBEDDING_BATCH_SIZE` | `10` | Texts per embedding request. |

## Video / Image Parameters

| Variable | Default | Description |
|---|---|---|
| `VIDEO_FPS` | `30` | Frame rate. |
| `VIDEO_WIDTH` / `VIDEO_HEIGHT` | `1080` / `1920` | Portrait resolution. |
| `VIDEO_CODEC` | `libx264` | Video codec (H.264); the ffmpeg build must include this encoder, see the [ffmpeg guide](../guides/ffmpeg.md). |
| `VIDEO_AUDIO_CODEC` | `aac` | Audio codec. |
| `VIDEO_BITRATE` | `4M` | Bitrate. |
| `IMAGE_DEFAULT_STYLE` | `chinese_period_drama` | Default image style. |

## Media Tools

| Variable | Default | Description |
|---|---|---|
| `FFMPEG_PATH` | `ffmpeg` (from PATH) | Path to the ffmpeg executable; specify explicitly when installed in a non-standard location. |

## Security

| Variable | Default | Description |
|---|---|---|
| `PROMPT_EXPORT_PASSWORD` | `change_me` | Prompt-export password; **always override it for deployment**. |
| `ST_COOKIE_SECURE` | `true` | Whether the admin cookie is Secure. Local HTTP development needs `0`, otherwise the browser drops the cookie. |

## Release Notifications

| Variable | Default | Description |
|---|---|---|
| `RELEASE_NOTIFICATIONS_ENABLED` | `true` | Set `false` to fully disable the release feed, including packaged notes parsing and GitHub checks. |
| `RELEASE_NOTIFICATIONS_GITHUB_TOKEN` | Empty | Optional GitHub token for a higher `releases/latest` rate limit. Anonymous requests are used when empty. |

## Observability Tracing (optional, off by default)

| Variable | Description |
|---|---|
| `NOVELVIDEO_ENABLE_LOGFIRE` | Enables PydanticAI tracing instrumentation. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Export traces to your own OTLP/Jaeger. |
| `LOGFIRE_TOKEN` | The only switch that sends data to the Logfire SaaS. |

See the [telemetry notes](../guides/telemetry.md) for details. By default none of the three are set, and no data is sent.

## Related

- Full list with comments: `.env.example` in the repo root
- [Quickstart](../getting-started/quickstart.md) ｜ [Configuring Model Providers](../getting-started/configuring-models.md) ｜ [Self-Hosting Manual](../guides/self-hosting.md)
