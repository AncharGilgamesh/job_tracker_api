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

`JWT_SECRET_KEY` in the example is intentionally too short to use as a signing secret. Generate a local value and store it only in `.env`:

```text
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`JWT_ALGORITHM` must be `HS256`. `ALLOWED_ORIGINS` is a comma-separated list of explicit browser origins, for example `http://127.0.0.1:3000`.

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

The process reads `ENVIRONMENT`, `DATABASE_URL`, `JWT_ALGORITHM`, `JWT_SECRET_KEY`, and `ALLOWED_ORIGINS` from the environment and from `.env` in the working directory. Startup validates those settings, creates an async database engine, and does not open a database connection.

Open `http://127.0.0.1:8000/health`. A running process returns:

```json
{"status": "ok"}
```

`/health` reports process liveness only. It does not open a database connection, execute SQL, or request a database session.

## HTTP API

The application routes below are the ones implemented by the routers. Error responses use this body:

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "The requested resource was not found",
    "details": []
  }
}
```

`code`, `message`, and `details` change with the failure. `details` is an empty array when the handler has nothing further to report. JSON request models reject unknown fields.

There is no user delete route.

List responses that exist are `{"items": [...]}`. Those routes do not paginate and do not take filter query parameters. `GET /companies/suggestions` is the one list that takes a query parameter: required `name`, described below.

### Health

`GET /health` does not require authentication. A running process returns:

```json
{"status": "ok"}
```

### Authentication

`POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, and `POST /auth/logout` do not read an `Authorization` header.

Login, refresh, and logout require the cookie CSRF check:

- `X-Requested-With` must be `XMLHttpRequest`.
- When `Origin` is present, it must match one entry in `ALLOWED_ORIGINS` exactly.
- When `Origin` is absent, the scheme and authority of `Referer` must match one entry in `ALLOWED_ORIGINS`.

A failed check returns 403 with code `CSRF_CHECK_FAILED` and message `Cross-site request blocked`. Register does not use this check.

`POST /auth/register` accepts `email` and `password`. The password must be 15 to 128 characters. Success is 200:

```json
{"message": "Registration request processed. You can now try signing in."}
```

The response includes no access token and sets no cookie. An email that is already registered returns that same message. An invalid email is 422 with code `VALIDATION_ERROR` and a `body.email` detail. A password outside the length rule is 422 with code `VALIDATION_ERROR` and a `body.password` detail whose code is `PASSWORD_POLICY_VIOLATION` and whose message is `Password must be between 15 and 128 characters`.

`POST /auth/login` accepts `email` and `password` and uses the CSRF check. Success is 200:

```json
{
  "access_token": "<token>",
  "token_type": "bearer",
  "expires_in": 900
}
```

The refresh token is not in that JSON. It is set as the `refresh_token` cookie: `HttpOnly`, `SameSite=Lax`, `Path=/auth`, `Max-Age=2592000`. The cookie is `Secure` when `ENVIRONMENT` is not `local`. An unknown email and a wrong password both return 401 with code `INVALID_CREDENTIALS` and message `Invalid email or password`. An invalid email is the same 422 as register.

`POST /auth/refresh` has no JSON body. It uses the CSRF check and reads the `refresh_token` cookie. Success rotates that cookie and returns the same JSON shape as login, with `expires_in` 900. A missing, empty, or rejected cookie returns 401 with code `INVALID_REFRESH_TOKEN` and message `Invalid refresh token`.

`POST /auth/logout` uses the CSRF check and returns 204 with an empty body. A refresh cookie that matches a stored token revokes that token's family. An unknown cookie revokes nothing. The response clears the `refresh_token` cookie in either case, including when the request sent no cookie.

### Bearer access

Company, vacancy, application, history, and interview routes require `Authorization: Bearer` with the access token from login or refresh. They do not use the cookie CSRF check. A missing bearer credential is 401 with code `MISSING_ACCESS_TOKEN` and message `Authentication is required`. An invalid token is 401 with code `INVALID_ACCESS_TOKEN` and message `Invalid access token`.

### Companies

Companies are a shared catalog. Authenticated users can read and create companies. Only the creator can rename or archive a company. A missing company is 404 with code `RESOURCE_NOT_FOUND`, message `The requested resource was not found`, and `details` `[]`. A non-creator rename or archive of an existing company is 403 with code `FORBIDDEN`, message `The request is forbidden`, and `details` `[]`.

A company object has `id`, `created_by_user_id`, `name`, `normalized_name`, and `archived_at`. `archived_at` is null while the company is active. The stored display name is Unicode NFKC, trimmed, with internal whitespace collapsed. `normalized_name` is that display name casefolded. A name that is empty after that normalization is 422 with code `VALIDATION_ERROR`.

- `GET /companies` returns every company, including archived companies, as `{"items": [...]}`.
- `GET /companies/suggestions` requires query parameter `name`. It returns companies whose normalized name equals the normalized query, including archived companies, as `{"items": [...]}`.
- `GET /companies/{company_id}` returns one company, active or archived.
- `POST /companies` accepts `{"name": "..."}` and returns 201. `created_by_user_id` is the authenticated user. `archived_at` is null. The same normalized name can exist on more than one row.
- `PATCH /companies/{company_id}` accepts `{"name": "..."}` and renames the company when the caller created it. `created_by_user_id` and `archived_at` stay as they are.
- `POST /companies/{company_id}/archive` has no body. The creator's first archive sets `archived_at` to the current UTC time and returns the company. A later archive keeps that original timestamp.

### Vacancies

Vacancy routes require `Authorization: Bearer`. They do not use the cookie CSRF check. `user_id` on a vacancy is the authenticated user. The request body cannot set it. A missing vacancy and another user's vacancy return the same 404 body: code `RESOURCE_NOT_FOUND`, message `The requested resource was not found`, `details` `[]`.

A vacancy object has `id`, `user_id`, `company_id`, `title`, `description`, `source_url`, and `location`. `source_url` is plain text.

- `POST /vacancies` returns 201. The body is `company_id`, `title`, and optional `description`, `source_url`, and `location`. `title` must contain a non-whitespace character. A missing company is 404 with `RESOURCE_NOT_FOUND`. An archived company is 409 with code `COMPANY_ARCHIVED`, message `The company is archived`, and `details` `[]`.
- `GET /vacancies` returns the caller's vacancies in id order as `{"items": [...]}`.
- `GET /vacancies/{vacancy_id}` returns one vacancy owned by the caller.
- `PATCH /vacancies/{vacancy_id}` updates the caller's vacancy. Send at least one of `company_id`, `title`, `description`, `source_url`, and `location`. Omitted keys stay unchanged. Explicit null clears `description`, `source_url`, and `location`. `company_id` and `title` cannot be null, and `title` cannot be blank. Changing `company_id` to a missing company is 404 with `RESOURCE_NOT_FOUND`. Changing it to an archived company is 409 with `COMPANY_ARCHIVED`.
- `DELETE /vacancies/{vacancy_id}` returns 204 when the owned vacancy has no application. When an application exists, it returns 409 with code `VACANCY_HAS_APPLICATION`, message `The vacancy has an application`, and `details` `[]`.

### Applications

The application router is mounted at `/vacancies`. These routes require `Authorization: Bearer` and do not use the cookie CSRF check. `GET /vacancies/{vacancy_id}/applications`, `PATCH /vacancies/{vacancy_id}/applications`, `DELETE /vacancies/{vacancy_id}/applications`, and `GET /vacancies/{vacancy_id}/applications/history` return the same 404 body when the vacancy is missing, belongs to another user, or is owned and has no application: code `RESOURCE_NOT_FOUND`, message `The requested resource was not found`, `details` `[]`.

An application object has `id`, `vacancy_id`, and `status`. Status values are `APPLIED`, `SCREENING`, `INTERVIEW`, `OFFER`, `REJECTED`, and `WITHDRAWN`.

- `POST /vacancies/{vacancy_id}/applications` returns 201. The JSON body is an empty object. The server creates the application as `APPLIED` and inserts one history row with `from_status` null and `to_status` `APPLIED`. A second application for that vacancy is 409 with code `APPLICATION_ALREADY_EXISTS`, message `An application already exists for this vacancy`, and `details` `[]`.
- `GET /vacancies/{vacancy_id}/applications` returns that one application object.
- `PATCH /vacancies/{vacancy_id}/applications` accepts `{"status": "<STATUS>"}`. A different status updates the application and appends one history row. The same status returns the application and leaves history unchanged. Any other status is 422 with code `VALIDATION_ERROR`.
- `GET /vacancies/{vacancy_id}/applications/history` returns `{"items": [...]}` ordered by `changed_at`, then `id`, oldest first. Each item has `id`, `application_id`, `from_status`, `to_status`, and `changed_at`.
- `DELETE /vacancies/{vacancy_id}/applications` requires `Authorization: Bearer` and does not use the cookie CSRF check. It returns 204 with an empty body when the owned application has no interview. The delete removes that application's history rows, then the application. The vacancy remains. When an interview exists, nothing is deleted and the response is 409 with code `APPLICATION_HAS_INTERVIEWS`, message `The application has an interview`, and `details` `[]`.

### Interviews

The interview router has no prefix. These routes require `Authorization: Bearer` and do not use the cookie CSRF check. A missing application or interview and another user's application or interview return the same 404 body: code `RESOURCE_NOT_FOUND`, message `The requested resource was not found`, `details` `[]`.

Interview fields are `scheduled_at`, `notes`, `outcome`, `status`, and `interview_type`. On create, `scheduled_at` is required. `notes`, `outcome`, `status`, and `interview_type` are optional. An omitted `status` or `interview_type` is stored as null. Explicit null is stored as null. `scheduled_at` must include a timezone offset on create and whenever PATCH sends it, and it is stored as UTC. A naive timestamp is rejected on both writes. `notes` and `outcome` are nullable. `outcome` is free text, not a fixed list. `status` is nullable. Non-null values are exactly `SCHEDULED`, `COMPLETED`, `CANCELLED`, and `NO_SHOW`. `interview_type` is nullable. Non-null values are exactly `PHONE`, `VIDEO`, and `ONSITE`. Any other status or interview type is 422 with code `VALIDATION_ERROR`, message `Request validation failed`, and one detail whose field is `body.status` or `body.interview_type`, whose code is `INVALID_VALUE`, and whose message is `The interview status is not supported` or `The interview type is not supported`. Creating or updating an interview leaves the application status and its history unchanged. Setting `status` or `interview_type` does not change the application status or its history.

An interview object has `id`, `application_id`, `scheduled_at`, `outcome`, `notes`, `status`, and `interview_type`.

- `POST /applications/{application_id}/interviews` returns 201. `scheduled_at` is required. `notes`, `outcome`, `status`, and `interview_type` are optional. Omitted notes and outcome are stored as null. An omitted `status` or `interview_type` is stored as null. Explicit null is stored as null.
- `GET /applications/{application_id}/interviews` returns the caller's interviews for that application in id order as `{"items": [...]}`. An owned application with no interviews returns an empty `items` array.
- `GET /interviews/{interview_id}` returns one interview owned through the caller's vacancy.
- `PATCH /interviews/{interview_id}` accepts at least one of `scheduled_at`, `notes`, `outcome`, `status`, and `interview_type`. An omitted key stays unchanged. Explicit null clears `notes`, `outcome`, `status`, and `interview_type`. `scheduled_at` cannot be null. An empty object is 422 with code `VALIDATION_ERROR`.
- `DELETE /interviews/{interview_id}` requires `Authorization: Bearer` and does not use the cookie CSRF check. It returns 204 with an empty body. A missing interview and another user's interview return the same 404 body: code `RESOURCE_NOT_FOUND`, message `The requested resource was not found`, `details` `[]`. Deleting an interview does not change the application status or its history.
