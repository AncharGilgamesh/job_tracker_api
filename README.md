# Job Tracker API

Job Tracker API is a multi-user backend for tracking a job search. Each user has private vacancies, applications, interviews, and status history. Companies belong to a shared catalog.

This repository currently contains the runnable application skeleton.

## Stack

- Python 3.12+
- uv
- FastAPI
- Pydantic v2
- Uvicorn
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

Install dependencies from the lockfile:

```text
uv sync
```

## Tests and Ruff

```text
uv run pytest
uv run ruff check .
```

## Run the application

```text
uv run uvicorn job_tracker.main:app --host 127.0.0.1 --port 8000
```

The process reads `ENVIRONMENT` and `DATABASE_URL` from the environment and from `.env` in the working directory. Open `http://127.0.0.1:8000/health`. A running process returns:

```json
{"status": "ok"}
```

`/health` reports process liveness only. It does not connect to PostgreSQL.

## Docker Compose

Copy `.env.example` to `.env` before starting the stack. Compose reads that file for the database credentials. The Compose file does not contain a database password.

```text
docker compose up --build
```

The API listens on port 8000. PostgreSQL 17 is available to the application on the hostname `db`. Stop the stack with:

```text
docker compose down
```
