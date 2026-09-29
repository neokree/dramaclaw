<!-- lang-switch -->
[English](../../en/getting-started/configuring-models.md) · **简体中文**

# 配置模型

DramaClaw CE 通过少数几个引擎生成文本、图片、视频和音频。本地引擎在本机运行，不产生费用；Higgsfield 在云端运行，按其自身积分计费。引擎通过 `.env` 中的环境变量选择（见 [`.env.example`](../../../.env.example)），不再需要维护逐个模型的映射。

| 环节 | 引擎 | 选择值 |
|---|---|---|
| 文本 / 视觉理解 | MTPLX（本地，默认）或 OpenRouter | `TEXT_ENGINE=mtplx` / `TEXT_ENGINE=openrouter` |
| 图片 | Draw Things（本地）、任意 Higgsfield 图片模型、OpenRouter 图片模型 | `drawthings` / `higgsfield:<模型>` / `openrouter:<模型>` |
| 视频 | 任意 Higgsfield 视频模型，或 h3.c / MiniMax H3（本地） | `higgsfield:<模型>` / `h3c` |
| 配音、音乐、音效 | Higgsfield | `HIGGSFIELD_TTS_*` |

启动后打开 `http://localhost:8080`，点击 **设置**。**引擎** 区块显示每个引擎是否可用、Higgsfield 剩余积分、MTPLX 是否在运行或按需启动，以及当前文本引擎。该区块读取 `GET /api/v1/model-gateway/engines`，只读：要更换引擎，请修改 `.env` 并重启 API。

## 文本：MTPLX 或 OpenRouter

`TEXT_ENGINE` 决定所有文本请求及带图的文本（视觉理解）请求使用的引擎。结构化抽取（角色、场景、道具）、规划元数据和 Hermes 聊天也跟随它。

### MTPLX（默认）

MTPLX 是本机的 OpenAI 兼容服务（`mtplx serve`）。如果 `MTPLX_BASE_URL` 上没有服务响应，DramaClaw 会在第一次文本请求时自动启动它，无需 key。默认模型带视觉塔，支持 `image_url` 输入。

| 变量 | 默认值 |
|---|---|
| `MTPLX_BASE_URL` | `http://127.0.0.1:8000/v1` |
| `MTPLX_MODEL` | `hawhyhb-qwen36-35b-a3b-uncensored-heretic-mtplx-4bit-fp16` |
| `MTPLX_BINARY` | `~/.mtplx/bin/mtplx` |
| `MTPLX_MODEL_PATH` | `~/.mtplx/models/hawhyhb--Qwen3.6-35B-A3B-Uncensored-Heretic-MTPLX-4bit-FP16` |
| `MTPLX_IDLE_SECONDS` | `120`（DramaClaw 自己启动的服务在最后一次文本请求或对话轮次结束后保留的秒数；`0` 表示立即停止） |

如果 `MTPLX_BASE_URL` 上已有服务响应但未提供 `MTPLX_MODEL`，文本请求会直接报错，不会再启动第二个服务。MTPLX 只服务一个模型，因此 `.env.example` 中按任务覆盖的 `*_MODEL` 在 MTPLX 下不生效。

### OpenRouter

设置 `TEXT_ENGINE=openrouter` 和 `OPENROUTER_API_KEY`。`OPENROUTER_MODEL` 为默认模型（留空时为 `google/gemma-4-26b-a4b-it`）。`CHARACTER_BUILD_MODEL`、`FREEZONE_VISION_MODEL` 等按任务覆盖只在 OpenRouter 下生效，取值为 `vendor/model` 形式；旧的 `DC-*` 逻辑名会被忽略。

`TEXT_TIMEOUT_SECONDS`（默认 300）设置文本 HTTP 超时。`TEXT_TRUST_ENV` 控制客户端是否读取系统代理：MTPLX 默认 `false`，OpenRouter 默认 `true`。

## Higgsfield（视频、图片、音频）

Higgsfield 通过其 CLI 调用。安装后先登录一次：

```bash
higgsfield auth login
higgsfield account status   # 查看积分余额
```

DramaClaw 依次在 `HIGGSFIELD_BINARY`、`PATH`、`/opt/homebrew/bin/higgsfield` 中查找 CLI。模型不写死在代码里：模型目录来自 `higgsfield model list`，每次请求按该模型的 schema（`higgsfield model get <job> --json`）组装，模型不接受的参数不会发送。模型目录和 schema 在磁盘上缓存一天，位置为 `HIGGSFIELD_CACHE_DIR`（默认 `~/.cache/dramaclaw/higgsfield`）。

Higgsfield 选择值为 `higgsfield:<model_ref>`，可以用查询串带上预设，例如 `higgsfield:kling3_0?mode=pro` 或 `higgsfield:gpt_image_2_5?variant=flare`；`mode` 和 `variant` 在不同模型中含义不同。推荐模型（Seedance、Kling 3.0、GPT Image、Nano Banana、Seedream）在选择器中排在前面，平台上的其他模型也通过同一套按 schema 组装的路径使用。

任务 id 会在等待结果之前写入磁盘，因此被中断的付费任务在重试时会重新接上，不会重复付费。CLI 不支持取消任务。

### 视频

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VIDEO_BACKEND` | 空 | `higgsfield:<模型>` 或 `h3c`。留空时使用 `HIGGSFIELD_VIDEO_MODEL`。 |
| `HIGGSFIELD_VIDEO_MODEL` | `seedance_2_0?mode=fast` | 默认视频模型，即后端 `higgsfield:seedance_2_0?mode=fast`。 |
| `HIGGSFIELD_VIDEO_MODE` | `fast` | Seedance 2.0 的 `fast` / `std`；`fast` 只出 480p/720p。 |
| `HIGGSFIELD_VIDEO_RESOLUTION` | `480p` | 默认分辨率。 |

已下线供应商留下的旧选择值会先回落到 `VIDEO_BACKEND`，再回落到 `HIGGSFIELD_VIDEO_MODEL`。

### 图片

| 变量 | 默认值 |
|---|---|
| `DEFAULT_IMAGE_SELECTION` | `higgsfield:nano_banana_flash` |
| `DEFAULT_CHARACTER_IMAGE_SELECTION` / `DEFAULT_SKETCH_IMAGE_SELECTION` / `DEFAULT_RENDER_IMAGE_SELECTION` | 同 `DEFAULT_IMAGE_SELECTION` |
| `DIRECTOR_SKETCH_IMAGE_SELECTION` / `DIRECTOR_SKETCH_IMAGE_QUALITY` | `higgsfield:gpt_image_2` / `low` |

以上变量均可取 `drawthings`、`higgsfield:<模型>` 或 `openrouter:<模型>`。比例、分辨率和质量会映射到所选模型 schema 接受的取值。

### 配音、音乐和音效

- 角色或解说人上传参考样本得到的声线走 `seed_audio`，样本作为音频参考。
- Higgsfield 自带声线（`higgsfield voices list`，预设或克隆）走 `HIGGSFIELD_TTS_MODEL`（默认 `text2speech_v2`），引擎为 `HIGGSFIELD_TTS_VARIANT`（默认 `elevenlabs`）。`HIGGSFIELD_TTS_VOICE` 设置默认声线 id；留空时使用第一个预设声线。
- 音乐使用 `sonilo_music`，音效使用 `mirelo_text_to_audio`。

## Draw Things（本地图片）

Draw Things 提供与 A1111 兼容的 HTTP API。在 Draw Things 中开启 **Settings → API Server**（HTTP，端口 7860），然后选择 `drawthings`，例如 `DEFAULT_IMAGE_SELECTION=drawthings`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DRAWTHINGS_URL` | `http://127.0.0.1:7860` | API 服务地址。 |
| `DRAWTHINGS_MODEL` | 空 | 留空时使用 Draw Things 当前加载的模型。 |
| `DRAWTHINGS_STEPS` | 空 | 留空时使用 Draw Things 当前设置。 |

第一张参考图作为 img2img 的初始图。

## OpenRouter 图片

设置 `OPENROUTER_API_KEY`，选择 `openrouter:<模型>`，例如 `openrouter:google/gemini-3.1-flash-image-preview`。`OPENROUTER_IMAGE_MODELS` 是选择器中提供的模型列表（逗号分隔）；留空时为 `google/gemini-3.1-flash-image-preview,openai/gpt-5.4-image-2`。参考图以 data URL 内联发送。

## h3.c（本地视频）

h3.c 在本机 Metal 上运行 MiniMax H3。只有二进制和权重都存在时，才会出现 `h3c` 后端：

| 变量 | 默认值 |
|---|---|
| `H3C_BINARY` | `~/Developer/AI-Tools/h3.c/h3` |
| `H3C_WEIGHTS` | `~/Developer/AI-Tools/h3.c/MiniMax-H3` |

h3.c 以 24 fps 出片，支持首帧和尾帧，比例支持 `9:16`、`16:9`、`1:1`、`4:3`、`3:4`、`4:5`，不接受图片、视频或音频参考。

## 积分与用量

Higgsfield 任务运行前，DramaClaw 会先报出积分价格。每个任务都会记入项目的用量台账，记录 Higgsfield 实际收取的积分；本地引擎（Draw Things、h3.c）费用为 0，OpenRouter 记录其响应中报告的美元费用（没有报告时为 0）。按项目查询：

- `GET /api/v1/projects/{project}/video-usage`：视频任务与已花费积分，以及 Higgsfield 余额。
- `GET /api/v1/projects/{project}/character-image-usage` 和 `GET /api/v1/projects/{project}/episodes/{episode}/sketch-image-usage`：图片请求。

## 常见问题

| 现象 | 检查方法 |
|---|---|
| Higgsfield 显示“不可用” | CLI 未安装或找不到（`HIGGSFIELD_BINARY`），或会话已过期：执行 `higgsfield auth login`。 |
| Draw Things 显示“不可用” | 在 Draw Things 中开启 **Settings → API Server**（HTTP，端口 7860），或修正 `DRAWTHINGS_URL`。 |
| 视频模型里没有 h3.c | `H3C_BINARY` 或 `H3C_WEIGHTS` 不存在。 |
| MTPLX 下文本请求失败 | `MTPLX_BINARY` 不存在，或 `MTPLX_BASE_URL` 上的其他服务提供的模型不是 `MTPLX_MODEL`。 |
| OpenRouter 显示“不可用” | 未设置 `OPENROUTER_API_KEY`。 |
| Docker 中访问不到 `127.0.0.1` 上的引擎 | 容器内的 `127.0.0.1` 指容器自身。把 `DRAWTHINGS_URL` / `MTPLX_BASE_URL` 改为后端可访问的宿主机地址。镜像中不包含 Higgsfield CLI 和 h3.c。 |

## 相关文件

- `src/novelvideo/engines/`：引擎驱动（Higgsfield、Draw Things、OpenRouter 图片、h3.c、MTPLX、音频）。
- `src/novelvideo/media_catalog.py`：根据已安装引擎生成的媒体模型目录。
- `.env.example`：环境变量参考。
- `docker-compose.yml`（源码构建）/ `docker-compose.release.yml`（镜像）：部署文件（api + web）。
- [自托管手册](../guides/self-hosting.md)
- [环境变量参考](../reference/environment-variables.md)
