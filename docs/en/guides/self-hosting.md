<!-- lang-switch -->
**English** · [简体中文](../../zh/guides/self-hosting.md)

# Self-Hosting Handbook (Docker)

> Deploy, configure, upgrade, and back up DramaClaw CE with Docker.

CE ships two containers: `api` + `web`, with **no PostgreSQL / no Redis / no Celery** (`ST_EDITION=ce`; tasks run inline within the process). Text runs on MTPLX (local) or OpenRouter, images on Draw Things (local), Higgsfield, or OpenRouter, video on Higgsfield or h3.c (local), and audio on Higgsfield.

Two compose files ship in the repo: `docker-compose.yml` builds both services from source (the default entry point — `docker compose up -d --build`), and `docker-compose.release.yml` only pulls published images (`docker compose -f docker-compose.release.yml up -d`). `docker-compose.yml` extends `docker-compose.release.yml` for the shared runtime definition (env / ports / volumes / healthchecks) and only adds `build:` plus local image names.

## 1. Prerequisites

- Docker + `docker compose`.
- Docker Compose ≥ 2.24 (`docker compose version`).
- Resources: ≥ 2 vCPU / 4GB recommended for the stack itself. The local engines (MTPLX, Draw Things, h3.c) run on the host and need their own hardware.
- The engines you plan to use: the Higgsfield CLI signed in with `higgsfield auth login`, and/or an `OPENROUTER_API_KEY`, and/or the local engines.

## 2. Get the compose file and configuration

```bash
git clone https://github.com/dramaclaw/dramaclaw.git
cd dramaclaw
cp .env.example .env
```

Two files, already set for you, no changes needed:

| File | Mode | Command |
|---|---|---|
| `docker-compose.yml` | Source build (default) — builds `api` and `web` from this checkout | `docker compose up -d --build` |
| `docker-compose.release.yml` | Image only — pulls published images, never builds | `docker compose -f docker-compose.release.yml up -d` |

Key points shared by both (defined once in `docker-compose.release.yml`, reused by `docker-compose.yml` via `extends`):

| Item | Value | Notes |
|---|---|---|
| Services | `api` + `web` | No PG/Redis |
| Images (release) | `${DRAMACLAW_IMAGE_PREFIX:-claymorelab}/...` | Pulled from Docker Hub; set `DRAMACLAW_IMAGE_PREFIX` in `.env` to use the ACR mirror (pinned tags only) |
| Versions (release) | `DRAMACLAW_VERSION` (api/web) | Default: the tag baked into the file |
| Ports | `${ST_API_PORT:-8780}:8780`, `${ST_WEB_PORT:-8080}:80` | REST API, web UI |
| Enforced environment | `ST_EDITION=ce`, control-plane/Redis/Celery cleared | CE mode cannot be downgraded |
| Data volume | `ce-data:/data` (output at `/data/output`) | Persists project databases, settings, and generated media |

## 3. Configure `.env`

> ⚠️ **Secret-type defaults (such as `PROMPT_EXPORT_PASSWORD=change_me`) must be changed.** For the engines, see [Model Configuration](#model-configuration).

Groups (each item is commented inline in `.env.example`): task envelope signing, Docker images and text engine, structured extraction, per-task text model overrides, image engine selection, video engines (Higgsfield / h3.c), audio (Higgsfield), video base parameters, UI, and data directories.

### Model Configuration

Engines are chosen in `.env` (see [Configuring Model Providers](../getting-started/configuring-models.md) for details):

- **Text**: `TEXT_ENGINE=mtplx` (default, local, started on demand) or `TEXT_ENGINE=openrouter` with `OPENROUTER_API_KEY`.
- **Images**: `DEFAULT_IMAGE_SELECTION` = `drawthings`, `higgsfield:<model>` (default `higgsfield:nano_banana_flash`), or `openrouter:<model>`.
- **Video**: `VIDEO_BACKEND` = `higgsfield:<model>` (default `higgsfield:seedance_2_0?mode=fast`) or `h3c`.
- **Audio**: Higgsfield (`HIGGSFIELD_TTS_*`).

The Higgsfield CLI and h3.c are not part of the Docker image, and inside a container `127.0.0.1` is the container itself: point `DRAWTHINGS_URL` / `MTPLX_BASE_URL` at an address of the host reachable from `api`. After startup, **Settings → Engines** shows which engines are reachable.

## 4. Start / Stop

```bash
docker compose up -d --build                              # source build: start (builds on first run)
docker compose -f docker-compose.release.yml up -d         # image mode: start (pulls images on first run)
docker compose ps                                          # status
docker compose logs -f api                                 # logs
docker compose down                                        # stop (keeps the data volume)
```

## 5. Where the data lives / Backup, restore & migrate

- Project databases, settings, and generated media live in the named volume `ce-data` (`/data` inside the container); generated media is written to `/data/output`. Removing or rebuilding a container keeps this volume. Only an explicit `docker compose down -v` removes it.
- Back up the data volume:

```bash
docker run --rm -v dramaclaw-ce_ce-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/ce-data-backup.tar.gz -C /data .
```

(The volume name is prefixed with the compose project name; run `docker volume ls` to confirm the actual name.)

- Restore, or move to a new machine — copy `ce-data-backup.tar.gz` to the target host, then unpack it back into the data volume (the `-v` mount creates the volume if it does not exist yet):

```bash
docker run --rm -v dramaclaw-ce_ce-data:/data -v "$PWD":/backup alpine \
  tar xzf /backup/ce-data-backup.tar.gz -C /data
```

Then bring the stack up as usual (`docker compose up -d`). The volume backup already includes generated media; back up `.env` separately.

## 6. Upgrades

Source build:

```bash
git pull
docker compose up -d --build
```

Image mode:

```bash
# edit .env: DRAMACLAW_VERSION=2.1.0
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
```

Your `.env` is never touched by the upgrade. The `ce-data` volume is reused.

The `DRAMACLAW_VERSION` default baked into `docker-compose.release.yml` is kept current by the release packaging workflow: after each packaging run that publishes a compose bundle it opens a PR here pinning the default to that release's tag, so pulling `main` picks up the latest defaults even if your `.env` sets nothing.

If an older release wrote media to `/app/output` inside the container, run the one-time migration **before** starting the new version (zip users: download `scripts/migrate_docker_output.py` from GitHub instead of `git pull`):

```bash
git pull
docker compose exec -T api python - < scripts/migrate_docker_output.py
docker compose up -d
```

On Windows PowerShell:

```powershell
git pull
Get-Content scripts/migrate_docker_output.py -Raw | docker compose exec -T api python -
docker compose up -d
```

The script copies only missing files, never overwrites or deletes the source, and backs up `projects.db` before updating project paths. Files that existed only in an already-removed container layer cannot be recovered from the data volume.

### Upgrading from a previous checkout

`docker-compose.selfhosted.yml` and `docker-compose.selfhosted.release.yml` have been removed. Use the two files above instead — the `api` / `web` service names and the `ce-data` volume are unchanged, so existing data carries over as-is. The bundled `newapi` gateway service is gone: a leftover `newapi-data` volume is no longer used, and the `NEWAPI_*`, `COGNEE_*`, `ST_NEWAPI_*` and `DRAMACLAW_GATEWAY_*` variables in an old `.env` are ignored.

| Before | Now |
|---|---|
| `docker compose -f docker-compose.selfhosted.yml up -d --build` | `docker compose up -d --build` |
| `docker compose -f docker-compose.selfhosted.release.yml up -d` | `docker compose -f docker-compose.release.yml up -d` |
| `.env`'s `ST_API_PORT` / `ST_WEB_PORT` / `INSTALL_WORLD` | Unchanged, still effective |

## 7. Troubleshooting

| Symptom | What to check |
|---|---|
| Container won't start | `docker compose logs api`; follow the startup error to the `.env` value, port, or data volume at fault |
| Port 8780 already in use | Change the left-hand value of `ports` in compose, e.g. `8888:8780` |
| Model call errors | Check **Settings → Engines**: the selected engines must show “Available”. See [Configuring Model Providers](../getting-started/configuring-models.md#troubleshooting) |

## Related

- [Quickstart](../getting-started/quickstart.md) ｜ [Configuring Model Providers](../getting-started/configuring-models.md)
