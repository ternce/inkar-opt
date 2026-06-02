# Multi-stage build: build React frontend, then run FastAPI backend.

# ---- Frontend build ----
FROM node:20-alpine AS frontend
WORKDIR /app/front

COPY front/package.json front/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY front/ ./
RUN npm run build

# ---- Backend runtime ----
FROM python:3.13-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (minimal)
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install --no-cache-dir -r backend/requirements.txt
RUN python -m playwright install --with-deps chromium

COPY backend/ ./backend/

# Copy built frontend into repo-like location so backend can find front/dist
COPY --from=frontend /app/front/dist ./front/dist

ENV ENVIRONMENT=prod \
    FRONTEND_DIST=/app/front/dist

EXPOSE 8000

WORKDIR /app/backend

CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
