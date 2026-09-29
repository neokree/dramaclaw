<!-- lang-switch -->
[English](../../en/guides/troubleshooting.md) · **简体中文**

# 排错

> 自托管 DramaClaw CE 时的常见故障与排查。先看日志:`docker compose logs -f api`,或本地开发时看 `novelvideo api` 的终端输出。

## 启动类

| 现象 | 排查 |
|---|---|
| **容器起不来 / 立即退出** | 查看 `docker compose logs api`，按启动日志定位配置、端口或数据卷问题。 |
| **`8780` 端口被占用** | 改 compose `ports` 左值,如 `8888:8780`;或停掉占用进程(`lsof -i :8780`)。 |
| **健康检查一直 unhealthy** | 探活打 `/api/v1/config`;若 API 自身报错,看启动日志定位真正异常。 |
| **本地开发起不来:Python 版本** | 需 **3.11–3.12**(`>=3.11,<3.13`)。`uv python pin 3.12` 或装对应版本后 `uv sync`。 |

## 模型 / 引擎类

| 现象 | 排查 |
|---|---|
| **模型调用全报错** | 打开 **设置 → 引擎**，确认 `.env` 中选择的引擎显示“可用”。详见[配置模型](../getting-started/configuring-models.md#常见问题)。 |
| **Higgsfield 不可用或报 `higgsfield auth login`** | 安装 Higgsfield CLI（或设置 `HIGGSFIELD_BINARY`），执行 `higgsfield auth login`；`higgsfield account status` 可查看积分余额。 |
| **Draw Things 不可用** | 在 Draw Things 中开启 **Settings → API Server**（HTTP，端口 7860），或修正 `DRAWTHINGS_URL`。 |
| **MTPLX 下文本失败** | `MTPLX_BINARY` 不存在，或 `MTPLX_BASE_URL` 上的其他服务提供的模型不是 `MTPLX_MODEL`。 |
| **结构化环节报 `Exceeded maximum output retries`**（角色抽取、剧本规划等），纯文本环节正常 | 文本模型没有返回 function/tool call。任务日志（v2.0.3 起）会打出重试提示和底层原因。使用 OpenRouter 时，在 `OPENROUTER_MODEL` 或按任务覆盖的 `*_MODEL` 中换成支持 tool calling 的模型。 |
| **文本模型超时** | 调大 `TEXT_TIMEOUT_SECONDS`(默认 300);本地引擎被系统代理拦截时设 `TEXT_TRUST_ENV=false`。 |

## 媒体 / ffmpeg 类

| 现象 | 排查 |
|---|---|
| **合成阶段报找不到 ffmpeg** | 本地开发需自行装 ffmpeg(Docker 已自带);或用 `FFMPEG_PATH` 指定路径。见 [ffmpeg 指南](ffmpeg.md)。 |
| **合成失败,提示编码器不可用** | 默认编码 `libx264`(H.264),你的 ffmpeg build 须包含它;或改 `VIDEO_CODEC`。 |
| **成片黑屏 / 时长异常** | 多为上游图片/音频产物缺失;回看前序环节日志,确认素材已生成。 |

## 数据 / 升级类

| 现象 | 排查 |
|---|---|
| **重建后数据没了** | 数据在命名卷 `ce-data`(容器内 `/data`)。`docker compose down` 保留卷,**别加 `-v`**(会删卷)。备份见 [自托管手册](self-hosting.md#5-数据在哪--备份)。 |
| **升级后报配置错误** | 源码构建（`docker-compose.yml`）：`git pull && docker compose up -d --build`。镜像模式（`docker-compose.release.yml`）：`docker compose -f docker-compose.release.yml pull && docker compose -f docker-compose.release.yml up -d`（如果在 `.env` 里钉了 `DRAMACLAW_VERSION`，先改版本号）。详见自托管手册 §6。 |

## world 特性(3DGS/SHARP)类

| 现象 | 排查 |
|---|---|
| **报 `FileNotFoundError` 指向 `BuilderGPT/...`** | 这些重特性脚本不在 CE 精简包内,纯文本→成片不需要;走 3D/体素流程才需补齐。 |
| **`uv sync --extra world` 安装失败** | 需用 uv(非 pip)以使依赖 override 生效;GPU 加速需 CUDA 环境,slim/CPU 环境仅 CPU 路径。 |

## 还没解决?

- 用法/想法 → [GitHub Discussions](https://github.com/dramaclaw/dramaclaw/discussions)
- 确认是 Bug → [提交 Bug](https://github.com/dramaclaw/dramaclaw/issues/new?template=bug_report.yml)(附日志、复现步骤、环境)
- 安全问题 → 勿走公开 issue,见 [SECURITY](../../../SECURITY.md)

## 相关

- [安装指南](../getting-started/installation.md) ｜ [快速开始](../getting-started/quickstart.md) ｜ [自托管手册](self-hosting.md)
