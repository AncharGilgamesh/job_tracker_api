# Job Tracker API

Job Tracker API is a multi-user backend for tracking a job search. Each user has private vacancies, applications, interviews, and status history. Companies belong to a shared catalog.

## Stack

- Python 3.12+
- uv
- FastAPI
- Pydantic v2
- Uvicorn
- SQLAlchemy 2.0 with asyncio
- Alembic
- asyncpg
- PostgreSQL 17
- Docker and Docker Compose
- Ruff
- Pytest

## Local setup

Install uv and Python 3.12 or newer.

Copy the environment template and keep the copy local:

```text
copy .env.example .env
```

On Unix-like shells:

```text
cp .env.example .env
```

`.env.example` contains placeholders. Put local values in `.env`. That file stays untracked.

Application and test URLs use the asyncpg SQLAlchemy driver:

```text
postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5433/job_tracker
postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5433/job_tracker_test
```

`DATABASE_URL` selects the application database. `TEST_DATABASE_URL` selects the separate integration-test database `job_tracker_test`. Tests reject any other driver, host, port, or database name.

Install dependencies from the lockfile:

```text
uv sync
```

## PostgreSQL 17

Docker Compose runs PostgreSQL 17 and publishes it on the host only at:

```text
127.0.0.1:5433
```

That port is for host-run commands and integration tests. The application container connects to hostname `db`, port `5432`, and database `job_tracker`. Compose sets that application URL. It does not pass `TEST_DATABASE_URL` into the application container.

Start the stack:

```text
docker compose up --build
```

The API listens on port 8000. Stop the stack with:

```text
docker compose down
```

Integration tests create `job_tracker_test` when it does not exist. They reset and migrate only that database.

## Tests and Ruff

```text
uv run --env-file .env pytest
uv run ruff check .
```

PostgreSQL must be listening on `127.0.0.1:5433` before the integration tests run. `uv run --env-file .env` loads `TEST_DATABASE_URL` into the test process.

## Migrations

Alembic is the only schema-change path. Application startup does not run migrations and does not create tables.

Alembic reads `ALEMBIC_DATABASE_URL`. It does not fall back to `DATABASE_URL` or `TEST_DATABASE_URL`. Set an asyncpg URL for the database you intend to change.

PowerShell:

```text
$env:ALEMBIC_DATABASE_URL = "postgresql+asyncpg://job_tracker:change-me@127.0.0.1:5433/job_tracker"
uv run alembic upgrade head
```

Unix-like shells:

```text
ALEMBIC_DATABASE_URL=postgresql+asyncpg://job_tracker:change-me@127.0.0.1:5433/job_tracker uv run alembic upgrade head
```

Replace the placeholder user and password with the local values. Use database `job_tracker_test` when applying a migration to the test database.

## Run the application

```text
uv run uvicorn job_tracker.main:app --host 127.0.0.1 --port 8000
```

The process reads `ENVIRONMENT` and `DATABASE_URL` from the environment and from `.env` in the working directory. Startup validates those settings, creates an async database engine, and does not open a database connection.

Open `http://127.0.0.1:8000/health`. A running process returns:

```json
{"status": "ok"}
```

`/health` reports process liveness only. It does not open a database connection, execute SQL, or request a database session.
