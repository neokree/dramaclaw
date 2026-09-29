<!-- lang-switch -->
[English](../../en/guides/self-hosting.md) · **简体中文**

# 自托管手册（Docker）

> 用 Docker 部署、配置、升级、备份 DramaClaw CE。

CE 两个容器：`api` + `web`，**无 PostgreSQL / 无 Redis / 无 Celery**（`ST_EDITION=ce`，任务在进程内 inline 执行）。文本用 MTPLX（本地）或 OpenRouter，图片用 Draw Things（本地）、Higgsfield 或 OpenRouter，视频用 Higgsfield 或 h3.c（本地），音频用 Higgsfield。

仓库里有两份 compose 文件：`docker-compose.yml` 源码构建两个服务（默认入口 —— `docker compose up -d --build`），`docker-compose.release.yml` 只拉已发布镜像（`docker compose -f docker-compose.release.yml up -d`）。`docker-compose.yml` 通过 `extends` 复用 `docker-compose.release.yml` 里的运行定义（env / 端口 / 卷 / 健康检查），自己只加 `build:` 与本地镜像名。

## 1. 前置

- Docker + `docker compose`。
- Docker Compose ≥ 2.24（`docker compose version` 确认）。
- 资源：服务栈本身建议 ≥ 2 vCPU / 4GB。本地引擎（MTPLX、Draw Things、h3.c）运行在宿主机上，需要各自的硬件。
- 计划使用的引擎：已执行 `higgsfield auth login` 的 Higgsfield CLI，和/或 `OPENROUTER_API_KEY`，和/或本地引擎。

## 2. 拿到 compose 与配置

```bash
git clone https://github.com/dramaclaw/dramaclaw.git
cd dramaclaw
cp .env.example .env
```

两份文件，已为你定好，无需改：

| 文件 | 模式 | 命令 |
|---|---|---|
| `docker-compose.yml` | 源码构建（默认）—— `api`、`web` 用本仓 checkout 构建 | `docker compose up -d --build` |
| `docker-compose.release.yml` | 只拉镜像，不构建 | `docker compose -f docker-compose.release.yml up -d` |

两者共享的关键点（定义在 `docker-compose.release.yml` 里，`docker-compose.yml` 通过 `extends` 复用）：

| 项 | 值 | 说明 |
|---|---|---|
| 服务 | `api` + `web` | 无 PG/Redis |
| 镜像（release） | `${DRAMACLAW_IMAGE_PREFIX:-claymorelab}/...` | 默认拉 Docker Hub；`.env` 设 `DRAMACLAW_IMAGE_PREFIX` 切到 ACR 镜像（只有钉 tag） |
| 版本（release） | `DRAMACLAW_VERSION`（api/web） | 默认为文件里写死的 tag |
| 端口 | `${ST_API_PORT:-8780}:8780`、`${ST_WEB_PORT:-8080}:80` | REST API、Web 界面 |
| 强制环境 | `ST_EDITION=ce`、清空 control-plane/Redis/Celery | CE 模式不可降级 |
| 数据卷 | `ce-data:/data`（输出为 `/data/output`） | 持久化项目数据库、设置和生成媒体 |

## 3. 配置 `.env`

> ⚠️ **密钥类默认值（如 `PROMPT_EXPORT_PASSWORD=change_me`）必须改。** 引擎配置见 [模型配置](#模型配置)。

分组（`.env.example` 内有逐项注释）：任务信封签名、Docker 镜像与文本引擎、结构化抽取、按任务覆盖的文本模型、图像引擎选择、视频引擎（Higgsfield / h3.c）、音频（Higgsfield）、视频基础参数、UI、数据目录。

### 模型配置

引擎在 `.env` 中选择（详见 [配置模型供应商](../getting-started/configuring-models.md)）：

- **文本**：`TEXT_ENGINE=mtplx`（默认，本地，按需启动）或 `TEXT_ENGINE=openrouter` 并设置 `OPENROUTER_API_KEY`。
- **图片**：`DEFAULT_IMAGE_SELECTION` 取 `drawthings`、`higgsfield:<模型>`（默认 `higgsfield:nano_banana_flash`）或 `openrouter:<模型>`。
- **视频**：`VIDEO_BACKEND` 取 `higgsfield:<模型>`（默认 `higgsfield:seedance_2_0?mode=fast`）或 `h3c`。
- **音频**：Higgsfield（`HIGGSFIELD_TTS_*`）。

Docker 镜像中不包含 Higgsfield CLI 和 h3.c，且容器内的 `127.0.0.1` 指容器自身：把 `DRAWTHINGS_URL` / `MTPLX_BASE_URL` 改为 `api` 可访问的宿主机地址。启动后，**设置 → 引擎** 会显示哪些引擎可用。

## 4. 起停

```bash
docker compose up -d --build                          # 源码构建：启动（首次会构建）
docker compose -f docker-compose.release.yml up -d    # 镜像模式：启动（首次拉镜像）
docker compose ps                                     # 状态
docker compose logs -f api                            # 日志
docker compose down                                   # 停止（保留数据卷）
```

## 5. 数据在哪 / 备份、恢复与迁移

- 项目数据库、设置和生成媒体都在命名卷 `ce-data`（容器内 `/data`）；生成媒体固定写入 `/data/output`。删除或重建容器不会删除该卷，只有显式执行 `docker compose down -v` 才会删除。
- 备份数据卷：

```bash
docker run --rm -v dramaclaw-ce_ce-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/ce-data-backup.tar.gz -C /data .
```

（卷名前缀随 compose 项目名，`docker volume ls` 确认实际名。）

- 恢复 / 搬到新机 —— 把 `ce-data-backup.tar.gz` 拷到目标机，反向解回数据卷（`-v` 挂载会在卷不存在时自动创建）：

```bash
docker run --rm -v dramaclaw-ce_ce-data:/data -v "$PWD":/backup alpine \
  tar xzf /backup/ce-data-backup.tar.gz -C /data
```

然后照常起服务（`docker compose up -d`）。数据卷备份已包含生成媒体；`.env` 仍需单独备份。

## 6. 升级

源码构建：

```bash
git pull
docker compose up -d --build
```

镜像模式：

```bash
# 编辑 .env：DRAMACLAW_VERSION=2.1.0
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
```

升级不会碰你的 `.env`；`ce-data` 卷原样复用。

`docker-compose.release.yml` 里内置的 `DRAMACLAW_VERSION` 默认值由发行包打包 workflow 维护：每次打包并发布 compose 发行包后，它会向本仓开 PR，把默认值钉到该次发布的 tag，即使 `.env` 里什么都没设，`git pull` 到 `main` 也能拿到最新默认值。

如果旧版本曾将媒体写入容器内 `/app/output`，请在启动新版本**之前**运行一次迁移（zip 用户没有 git 仓库，直接从 GitHub 下载 `scripts/migrate_docker_output.py`）：

```bash
git pull
docker compose exec -T api python - < scripts/migrate_docker_output.py
docker compose up -d
```

Windows PowerShell 使用：

```powershell
git pull
Get-Content scripts/migrate_docker_output.py -Raw | docker compose exec -T api python -
docker compose up -d
```

脚本只复制缺失文件，不覆盖、不删除源文件，并在更新项目路径前备份 `projects.db`。只存在于已删除容器层里的文件无法从数据卷恢复。

### 从旧 checkout 升级

`docker-compose.selfhosted.yml` 与 `docker-compose.selfhosted.release.yml` 已移除，改用上面两份文件——`api` / `web` 服务名与 `ce-data` 数据卷不变，已有数据原样复用。内置的 `newapi` 网关服务已移除：遗留的 `newapi-data` 卷不再使用，旧 `.env` 里的 `NEWAPI_*`、`COGNEE_*`、`ST_NEWAPI_*`、`DRAMACLAW_GATEWAY_*` 变量会被忽略。

| 以前 | 现在 |
|---|---|
| `docker compose -f docker-compose.selfhosted.yml up -d --build` | `docker compose up -d --build` |
| `docker compose -f docker-compose.selfhosted.release.yml up -d` | `docker compose -f docker-compose.release.yml up -d` |
| `.env` 里的 `ST_API_PORT` / `ST_WEB_PORT` / `INSTALL_WORLD` | 含义不变，继续生效 |

## 7. 排错

| 现象 | 排查 |
|---|---|
| 容器起不来 | `docker compose logs api`；按启动报错定位到出问题的 `.env` 值、端口或数据卷 |
| 8780 端口占用 | 改 compose `ports` 左值，如 `8888:8780` |
| 模型调用报错 | 查看 **设置 → 引擎**：所选引擎须显示“可用”。见 [配置模型供应商](../getting-started/configuring-models.md#常见问题) |

## 相关

- [快速开始](../getting-started/quickstart.md) ｜ [配置模型供应商](../getting-started/configuring-models.md)
