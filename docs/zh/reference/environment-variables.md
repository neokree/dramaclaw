<!-- lang-switch -->
[English](../../en/reference/environment-variables.md) · **简体中文**

# 环境变量参考

> 配置通过项目根目录 `.env` 注入(从 `.env.example` 复制)。**`.env.example` 是权威全集且有逐项注释**;本篇按组归纳最常用的变量与默认值,便于查阅。

## 运行环境与路径

| 变量 | 默认 | 说明 |
|---|---|---|
| `ST_EDITION` | `ce`(compose/镜像强制) | 版本标识。CE 模式不可降级。 |
| `NOVELVIDEO_DATA_ROOT` | `.`(Docker 设为 `/data`) | 数据根目录,下面三项默认派生于此。 |
| `NOVELVIDEO_OUTPUT_DIR` | `$DATA_ROOT/output` | 成片与产物输出目录。Docker Compose 固定为 `/data/output`，随 `ce-data` 卷持久化。 |
| `NOVELVIDEO_STATE_DIR` | `$DATA_ROOT/state` | 本地状态。 |
| `NOVELVIDEO_RUNTIME_DIR` | `$DATA_ROOT/runtime` | 运行时临时目录。 |
| `ST_CONTROL_PLANE_DSN` / `ST_REDIS_URL` / `ST_CELERY_BROKER_URL` / `ST_CELERY_RESULT_BACKEND` | 空(CE 强制清空) | EE/分布式才用;CE 任务进程内 inline 执行,留空。 |

## 生成引擎

引擎在这里选择，不在网页中配置；**设置 → 引擎** 只显示各引擎的状态。详见[配置模型](../getting-started/configuring-models.md)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TEXT_ENGINE` | `mtplx` | 文本与视觉理解引擎：`mtplx`（本地）或 `openrouter`。 |
| `MTPLX_BASE_URL` / `MTPLX_MODEL` | `http://127.0.0.1:8000/v1` / 见 `.env.example` | 本机 MTPLX 服务，按需启动。`MTPLX_BINARY` 和 `MTPLX_MODEL_PATH` 指定二进制与权重位置。 |
| `OPENROUTER_API_KEY` | 空 | OpenRouter 文本（`TEXT_ENGINE=openrouter`）和 OpenRouter 图片使用的 key。 |
| `OPENROUTER_MODEL` | `google/gemma-4-26b-a4b-it` | OpenRouter 默认文本模型。按任务覆盖的 `*_MODEL`（`vendor/model` 形式）只在 OpenRouter 下生效。 |
| `OPENROUTER_IMAGE_MODELS` | `google/gemini-3.1-flash-image-preview,openai/gpt-5.4-image-2` | 以 `openrouter:<模型>` 提供的 OpenRouter 图片模型。 |
| `TEXT_TIMEOUT_SECONDS` | `300` | 文本模型 HTTP 超时(秒)。 |
| `TEXT_TRUST_ENV` | MTPLX 为 `false`，OpenRouter 为 `true` | 文本客户端是否读取系统代理。 |
| `DEFAULT_IMAGE_SELECTION` | `higgsfield:nano_banana_flash` | 默认图片模型：`drawthings`、`higgsfield:<模型>` 或 `openrouter:<模型>`。`DEFAULT_CHARACTER_IMAGE_SELECTION`、`DEFAULT_SKETCH_IMAGE_SELECTION`、`DEFAULT_RENDER_IMAGE_SELECTION` 按环节覆盖。 |
| `VIDEO_BACKEND` | 空 | 默认视频后端：`higgsfield:<模型>`（如 `higgsfield:kling3_0?mode=pro`）或 `h3c`。留空时使用 `HIGGSFIELD_VIDEO_MODEL`。 |
| `HIGGSFIELD_VIDEO_MODEL` / `HIGGSFIELD_VIDEO_MODE` / `HIGGSFIELD_VIDEO_RESOLUTION` | `seedance_2_0?mode=fast` / `fast` / `480p` | Higgsfield 默认视频模型与参数。 |
| `HIGGSFIELD_BINARY` | 先查 `PATH`，再用 `/opt/homebrew/bin/higgsfield` | Higgsfield CLI；需先执行 `higgsfield auth login`。 |
| `HIGGSFIELD_CACHE_DIR` | `~/.cache/dramaclaw/higgsfield` | 模型目录与 schema 缓存（一天）。 |
| `HIGGSFIELD_TTS_MODEL` / `HIGGSFIELD_TTS_VARIANT` / `HIGGSFIELD_TTS_VOICE` | `text2speech_v2` / `elevenlabs` / 第一个预设声线 | 使用 Higgsfield 声线配音。参考样本声线走 `seed_audio`。 |
| `DRAWTHINGS_URL` | `http://127.0.0.1:7860` | Draw Things API 服务。`DRAWTHINGS_MODEL`、`DRAWTHINGS_STEPS` 留空时用 Draw Things 当前设置。 |
| `H3C_BINARY` / `H3C_WEIGHTS` | `~/Developer/AI-Tools/h3.c/h3` / `~/Developer/AI-Tools/h3.c/MiniMax-H3` | 本地 h3.c 二进制与 MiniMax H3 权重。 |

## Embedding 网关

内置 NewAPI 现在只承载 Cognee 的 embedding 模型（`DC-cognee-embedding`）。其渠道、地址和 token 通过 `/api/v1/model-gateway` API 写入本机 `settings.db`，不通过环境变量配置。

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEWAPI_PROVISIONER_ENABLED` | `true` | 启用内置 NewAPI 一键初始化。 |
| `NEWAPI_ADMIN_BASE_URL` | `http://127.0.0.1:3000`（`.env.example` 中设置） | NewAPI 管理地址，不带 `/v1`。 |
| `COGNEE_EMBEDDING_MODEL` / `COGNEE_EMBEDDING_DIM` | `DC-cognee-embedding` / `1024` | 默认 embedding 模型与维度；各项目在 `project_config.json` 中保存自己的值。 |
| `EMBEDDING_BATCH_SIZE` | `10` | 每次 embedding 请求的文本条数。 |

## 视频 / 图像参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `VIDEO_FPS` | `30` | 帧率。 |
| `VIDEO_WIDTH` / `VIDEO_HEIGHT` | `1080` / `1920` | 竖屏分辨率。 |
| `VIDEO_CODEC` | `libx264` | 视频编码(H.264);ffmpeg build 须含此编码器,见 [ffmpeg 指南](../guides/ffmpeg.md)。 |
| `VIDEO_AUDIO_CODEC` | `aac` | 音频编码。 |
| `VIDEO_BITRATE` | `4M` | 码率。 |
| `IMAGE_DEFAULT_STYLE` | `chinese_period_drama` | 图像默认风格。 |

## 媒体工具

| 变量 | 默认 | 说明 |
|---|---|---|
| `FFMPEG_PATH` | `ffmpeg`(从 PATH) | ffmpeg 可执行路径,装在非标准位置时显式指定。 |

## 安全

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROMPT_EXPORT_PASSWORD` | `change_me` | 提示词导出口令,**部署务必覆盖**。 |
| `ST_COOKIE_SECURE` | `true` | 管理 Cookie 是否 Secure。本机 HTTP 开发需设 `0`,否则浏览器丢弃 cookie。 |

## 版本更新通知

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELEASE_NOTIFICATIONS_ENABLED` | `true` | 设为 `false` 可完全关闭 release feed,包括包内 notes 解析与 GitHub 检查。 |
| `RELEASE_NOTIFICATIONS_GITHUB_TOKEN` | 空 | 可选 GitHub token,用于提高 `releases/latest` 限流额度;留空走匿名请求。 |

## 可观测追踪(可选,默认关闭)

| 变量 | 说明 |
|---|---|
| `NOVELVIDEO_ENABLE_LOGFIRE` | 打开 PydanticAI 追踪埋点。 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | 导出 trace 到你自己的 OTLP/Jaeger。 |
| `LOGFIRE_TOKEN` | 唯一会把数据发往 Logfire SaaS 的开关。 |

详见 [遥测说明](../guides/telemetry.md)。默认三者都不设,不发送任何数据。

## 相关

- 完整列表与注释:仓库根 `.env.example`
- [快速开始](../getting-started/quickstart.md) ｜ [配置模型供应商](../getting-started/configuring-models.md) ｜ [自托管手册](../guides/self-hosting.md)
