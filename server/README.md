# Backend for File Upload App

This backend is a simple FastAPI service for user auth and file uploads. It is meant to work with a frontend app such as React/Vite.

## What this backend does

- User registration and login
- JWT-based auth
- File upload flow with chunk support
- Upload list and finalize endpoints

## Requirements

Before running the backend, make sure you have:

- Python 3.14+
- Redis running on localhost:6379
- PostgreSQL running and accessible

## 1. Create environment file

Create a file named `.env` in the `server` folder.

Example:

```env
Connection_String=postgresql+psycopg2://DB_USERNAME:DB_PASSWORD@localhost:5432/DB_NAME
FRONTEND_URL=http://localhost:5173
```

You can change the database value to match your local setup.

## 2. Install dependencies

From the `server` folder, run:


```bash
pip install -r requirements.txt
```

Or with `uv`:

```bash
uv sync
```

## 3. Run the backend

From the `server/src` folder:

```bash
uv run uvicorn main:app --reload --host 127.0.0.1 --port 8000
                            or
uv run src/main.py
```

The API will be available at:

- http://127.0.0.1:8000
- Health check: http://127.0.0.1:8000/

## 4. Main API endpoints

### Auth

- POST `/auth/register`
- POST `/auth/login`
- POST `/auth/logout`
- POST `/auth/refresh`
- GET `/auth/me`

### File upload

- POST `/v1/upload/init`
- POST `/v1/upload/chunk`
- POST `/v1/upload/finalize`
- GET `/v1/uploads`

## 5. Frontend usage notes

For frontend integration:

- Send the access token in the `Authorization` header as `Bearer <token>`.
- The backend already allows `http://localhost:5173` by default.
- If your frontend runs on another port, update `FRONTEND_URL` in `.env`.

## 6. Common issue

If the backend fails to start, check these first:

- Redis is running
- Database connection string is correct
- `.env` file exists in the `server` folder
