# Production image for Munshi's backend (FastAPI + uvicorn + the Pipecat
# voice pipeline) -- built for a container host like Render, not a
# serverless/functions target. Build context is server/ itself:
#   docker build -t munshi-server server/
# This does NOT build client/ -- that's a separate static-site deploy
# (Vercel); see ../DEPLOYMENT.md.
#
# Multi-stage: dependencies are installed into a venv in the builder stage so
# the runtime image never carries pip's build cache. requirements.txt is kept
# separate from pyproject.toml so this build never has to resolve this
# project's own flat-module layout (server.py, admin.py, etc. -- no
# packages/) as an installable package, only its third-party dependencies.

FROM python:3.12-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Everything server.py and its local imports need to run, plus the committed
# demo dataset the calculation engine reads from (DATA_DIR defaults to
# data/baseline_seed42, resolved relative to this directory -- see
# engine/data_loader.py's _data_dir()). .dockerignore keeps .venv/, .env,
# query_log.db and any other local dataset out of the image.
COPY . .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Non-root: defense in depth, no reason this process needs root inside the
# container.
RUN useradd --create-home --shell /bin/bash munshi && chown -R munshi:munshi /app
USER munshi

# $PORT is set by the platform (Render injects it); 8010 is the local/
# docker-run fallback, matching server/.env.example's dev default. EXPOSE is
# documentation only -- the entrypoint binds to whichever port is live.
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"8010\")}/health', timeout=3)" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
