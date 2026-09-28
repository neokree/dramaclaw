<!-- lang-switch -->
**English** · [简体中文](../../zh/getting-started/quickstart.md)

# Quickstart

> Run DramaClaw locally and produce your first result.

DramaClaw is the Community Edition (CE): it runs on a single machine with no PostgreSQL / Redis required. By default `docker compose` brings up three services: `api` (the creation backend, :8780), `newapi` (the bundled gateway, used only for knowledge-graph embeddings), and `web` (the browser UI, :8080). Generation runs on local engines (MTPLX for text, Draw Things for images, h3.c for video) and on Higgsfield in the cloud (video, images, voices, music), with OpenRouter as an alternative for text and images.

## Prerequisites

- Docker (Desktop or Engine) with `docker compose` support.
- The engines you want to use: the **Higgsfield CLI** signed in with `higgsfield auth login`, an `OPENROUTER_API_KEY`, and/or the local engines (MTPLX, Draw Things, h3.c).
- For knowledge-graph embeddings: a **DC key** (sign up / purchase at <https://relayclaw.cdnfg.com>) or an embedding channel in the NewAPI bundled with CE.

## Steps

```bash
# 1. Get the code — DramaClaw and the bundled gateway, side by side
git clone https://github.com/dramaclaw/dramaclaw.git
git clone https://github.com/dramaclaw/dramaclaw-gateway.git
cd dramaclaw

# 2. Prepare configuration
cp .env.example .env
#    Open .env and at minimum change PROMPT_EXPORT_PASSWORD to a non-default value.
#    Choose the engines in .env (TEXT_ENGINE, DEFAULT_IMAGE_SELECTION, VIDEO_BACKEND, OPENROUTER_API_KEY...).

# 3. Start — brings up api / newapi / web
docker compose up -d --build   # builds api, web (this checkout) and the gateway (../dramaclaw-gateway) from source
# no build? docker compose -f docker-compose.release.yml up -d   # pulls published images, no gateway clone needed

# 4. Confirm it's up
docker compose ps   # api, newapi, and web should all be running
```

## Check the engines

1. Open **`http://localhost:8080`** in your browser — this is the DramaClaw UI.
2. Open **Settings**. The **Engines** section shows which engines are reachable, the remaining Higgsfield credits, and the active text engine.
3. An engine marked “Unavailable” shows the reason. Fix it in `.env` (or sign in with `higgsfield auth login`) and restart the API.

> CE defaults to no-login, single local user (`ST_EDITION=ce`, enforced by compose). The REST API lives at `http://localhost:8780` (the browser only talks to `web`, which reverse-proxies to `api`).

## Embeddings

The knowledge graph needs the `DC-cognee-embedding` model from the bundled NewAPI. Save a DC key with `POST /api/v1/model-gateway/official/config`, or initialize the bundled NewAPI and add an embedding channel. The address and runtime token are stored in local `settings.db`, not `.env`. See [Configuring Model Providers](configuring-models.md#embedding-bundled-newapi).

## Next steps

- Full deployment/upgrade/backup: [Self-Hosting Handbook](../guides/self-hosting.md)
- Connect your own models: [Configuring Model Providers](configuring-models.md)
