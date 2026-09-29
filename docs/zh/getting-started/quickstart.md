<!-- lang-switch -->
[English](../../en/getting-started/quickstart.md) · **简体中文**

# 快速开始

> 本地跑起 DramaClaw,产出第一个结果。

DramaClaw 是社区版(CE),单机运行、无需 PostgreSQL / Redis。默认 `docker compose` 起两个服务:`api`(创作后端,:8780)、`web`(浏览器界面,:8080)。生成用本地引擎(文本 MTPLX、图片 Draw Things、视频 h3.c)和云端 Higgsfield(视频、图片、配音、音乐),文本和图片也可改用 OpenRouter。

## 前置

- Docker(Desktop 或 Engine),支持 `docker compose`。
- 要使用的引擎：已执行 `higgsfield auth login` 的 **Higgsfield CLI**、`OPENROUTER_API_KEY`，和/或本地引擎（MTPLX、Draw Things、h3.c）。

## 步骤

```bash
# 1. 取得代码
git clone https://github.com/dramaclaw/dramaclaw.git
cd dramaclaw

# 2. 准备配置
cp .env.example .env
#    打开 .env,至少把 PROMPT_EXPORT_PASSWORD 改成非默认值。
#    在 .env 中选择引擎（TEXT_ENGINE、DEFAULT_IMAGE_SELECTION、VIDEO_BACKEND、OPENROUTER_API_KEY 等）。

# 3. 启动 —— 起 api / web 两个服务
docker compose up -d --build   # 从本仓源码构建 api、web
# 免构建：docker compose -f docker-compose.release.yml up -d   # 拉已发布镜像

# 4. 确认已起
docker compose ps   # api、web 均应 running
```

## 检查引擎

1. 浏览器打开 **`http://localhost:8080`** —— 这就是 DramaClaw 的界面。
2. 打开 **设置**。**引擎** 区块显示哪些引擎可用、Higgsfield 剩余积分，以及当前文本引擎。
3. 显示“不可用”的引擎会给出原因。在 `.env` 中修正（或执行 `higgsfield auth login` 登录）后重启 API。

> CE 默认免登录、单本地用户(`ST_EDITION=ce`,compose 已强制)。REST API 在 `http://localhost:8780`(浏览器只与 `web` 通信,它再反代到 `api`)。

## 下一步

- 完整部署/升级/备份:[自托管手册](../guides/self-hosting.md)
- 接入自己的模型:[配置模型供应商](configuring-models.md)
