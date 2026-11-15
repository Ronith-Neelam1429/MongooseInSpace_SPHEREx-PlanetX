# Deploying the backend

The API (and optionally the survey worker) runs from the root `Dockerfile` on Render
(`render.yaml`). The frontend stays on GitHub Pages and calls the API.

## Why one service

A Render persistent disk attaches to one service only, and the survey queue is a SQLite
file in WAL mode, which needs shared memory and cannot live on a network filesystem. A
separate background worker could therefore never see the API's database. Instead the
container entrypoint (`deploy/entrypoint.sh`) starts the worker (`python -m planetx.survey
run --processor motion`, restarted every 5 minutes if it exits) next to uvicorn when
`PLANETX_RUN_WORKER=1`. Trade-off: the crawler shares CPU/RAM with the API. If that hurts,
set `PLANETX_RUN_WORKER=0` and run the worker elsewhere against its own database.

## Steps

1. Push this branch to `main` on GitHub.
2. Render dashboard -> New -> Blueprint -> pick the repo. It reads `render.yaml` and
   creates `planetx-api` (Starter plan plus a 10 GB disk; free instances cannot have disks).
3. If you rename the service, or your Pages URL differs from
   `https://ronith-neelam1429.github.io`, edit `PLANETX_CORS_ORIGINS` (origin only, no
   path; comma-separate several).
4. When the first deploy is healthy, open `https://<service>.onrender.com/api/health`
   and `/docs`.
5. GitHub repo -> Settings -> Secrets and variables -> Actions -> Variables -> new
   repository variable `VITE_API_BASE` = `https://<service>.onrender.com/api`
   (no trailing slash needed).
6. Re-run the "Deploy frontend to GitHub Pages" workflow (Actions -> Run workflow).
   Without the variable the build still succeeds; the site just has no API.

No secrets are needed. First boot runs `planetx.survey init` automatically.

## Operating

- Logs: Render dashboard -> planetx-api -> Logs (API and worker interleaved).
- Survey progress: `python -m planetx.survey status` in the Render shell.
- Cache is capped by `PLANETX_CACHE_MAX_MB` (6144); the disk also holds the DB.
- Rollback: Render -> Events/Deploys -> Rollback to a previous deploy. Disk data is kept.
  Frontend: re-run the Pages workflow on an earlier commit, or unset `VITE_API_BASE`.

## Results files

The image copies the tracked `results/` directory to `/app/results` and sets
`PLANETX_RESULTS_DIR=/app/results`; `GET /api/results/{name}` must read that variable (default:
the repo's `results/`), because the installed package no longer sits next to the repo. New result
files need a new image (push to `main`); they are not written at runtime.

## Before the demo

- A Starter instance has 512 MB of RAM and a motion run holds about 54 frames: watch the first
  "Find what moves" in the logs, and move to a larger plan if the service restarts (not measured).
- Fill the disk cache from your laptop: `uv run python tools/prewarm.py --base https://<service>.onrender.com`
  (docs/DEMO.md). First start of the container can take a minute (the health check waits 60 s).

## Local check

No Docker was available when this was written, so the image itself is unbuilt. What was run instead
is its exact install step (`uv sync --locked --no-dev --no-editable`, non-editable package) plus
`deploy/entrypoint.sh` as a non-root user, `/api/health`, `/docs` and `planetx.survey init`.
With Docker:

```
docker build -t planetx-api .
docker run --rm -p 8000:8000 -v planetx-data:/data planetx-api
curl http://127.0.0.1:8000/api/health
```
