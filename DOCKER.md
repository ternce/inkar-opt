# Docker Compose

This compose setup runs the built frontend through the FastAPI backend and uses PostgreSQL as the Docker database.

## Services

- `backend`: FastAPI app, frontend static files included from `front/dist`
- `postgres`: PostgreSQL 16, primary storage for Docker
- `redis`: Redis 7, prepared via `REDIS_URL` for future jobs/cache/locks usage

## Start

```powershell
Copy-Item .env.docker.example .env
docker compose up --build
```

Open:

```text
http://localhost:8000
http://localhost:8000/health
```

## Check PostgreSQL

```powershell
docker compose exec postgres psql -U apteka -d apteka -c "\dt"
docker compose exec postgres psql -U apteka -d apteka -c "select count(*) from price_formats;"
```

The backend creates tables on startup through SQLAlchemy `create_all` plus compatibility column/index checks.

## Check Redis

```powershell
docker compose exec redis redis-cli ping
```

Expected:

```text
PONG
```

## Check Backend Environment

```powershell
docker compose exec backend python -c "from app.db import get_database_url; from app.config import get_settings; print(get_database_url()); print(get_settings().redis_url)"
```

Expected database host in Docker:

```text
postgresql+psycopg://apteka:apteka@postgres:5432/apteka
redis://redis:6379/0
```

## Refresh Price Lists

Start from the UI:

1. Open `http://localhost:8000`
2. Go to `Назначение прайс-листов конкурентов`
3. Click `Обновить цены`

Or via API after selecting a real format code:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/api/price-formats/TEST_01/competitor-price-lists/refresh `
  -ContentType application/json `
  -Body '{"forceRefresh": true}'
```

Then check the returned `job_id`:

```powershell
Invoke-RestMethod http://localhost:8000/api/jobs/<job_id>
```

## Stop

```powershell
docker compose down
```

To remove Docker database/cache volumes:

```powershell
docker compose down -v
```

## Local Development

Running the backend outside Docker still uses the existing SQLite fallback when `DATABASE_URL` is not set. Docker always sets `DATABASE_URL` to PostgreSQL, so refresh/import inside Docker does not use SQLite.
