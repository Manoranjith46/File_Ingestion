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
 
 ### Organizations & Multi-Tenancy (Auth0 Aligned)
 
 - POST `/v1/organizations` *(Super Admin)*: Create a new organization tenant.
 - GET `/v1/organizations` *(Super Admin)*: List platform organizations.
 - GET `/v1/organizations/{id}` *(Super Admin)*: Get organization details.
 - PATCH `/v1/organizations/{id}` *(Super Admin)*: Suspend/reactivate organization or update Auth0 ID.
 - GET `/v1/organizations/me/profile` *(Org Admin / Member)*: View active organization profile.
 - PATCH `/v1/organizations/me/settings` *(Org Admin)*: Toggle Google SSO and update display name.
 - GET `/v1/organizations/me/members` *(Org Admin)*: List members in active organization.
 - POST `/v1/organizations/me/members` *(Org Admin)*: Add user to active organization (`org_admin` | `member`).
 - PATCH `/v1/organizations/me/members/{id}` *(Org Admin)*: Change member role.
 - DELETE `/v1/organizations/me/members/{id}` *(Org Admin)*: Remove member from active organization.
 
 ### File upload & Datasets (Tenant Scoped)
 
 - POST `/v1/datasets`: Create dataset scoped to active organization.
 - GET `/v1/datasets`: List datasets for active organization.
 - GET `/v1/datasets/{id}`: Get dataset and file tree (isolated to organization).
 - PATCH `/v1/datasets/{id}`: Update dataset metadata within active organization.
 - DELETE `/v1/datasets/{id}`: Remove dataset and associated files.
 - POST `/v1/datasets/{id}/files`: Attach uploaded file to dataset (enforces tenant ownership).
 - POST `/v1/upload/init`: Initialize chunked upload session (records tenant scope).
 - POST `/v1/upload/chunk`: Upload chunk with hash validation.
 - POST `/v1/upload/finalize`: Finalize and persist file scoped to organization.
 - GET `/v1/uploads`: List uploaded files tree for active organization.
 - POST `/v1/uploads/delete`: Delete uploaded file within active organization.
 
 ## 5. Authentication & Frontend usage notes
 
 For frontend integration:
 
 - **Auth0 Tokens**: Send standard Auth0 RS256 Bearer access token in `Authorization: Bearer <token>`.
 - **Organization Context**:
   - Clients specify the active organization context using the `X-Organization-ID` header (passing either internal UUID or Auth0 Org ID `org_xxx`).
   - If omitted, the backend defaults to the user's active organization membership.
 - **Cross-Tenant IDOR/BOLA Protection**: The backend verifies database membership for the authenticated user in the requested organization. Attempts to access other organizations' datasets or files return `403 Forbidden` or `404 Not Found`.
 - **Super Admin Safeguard**: Platform Super Admins manage organizations and configuration, but cannot query or mutate customer tenant datasets without explicit organization membership.
- The backend already allows `http://localhost:5173` by default.
- If your frontend runs on another port, update `FRONTEND_URL` in `.env`.

## 6. Common issue

If the backend fails to start, check these first:

- Redis is running
- Database connection string is correct
- `.env` file exists in the `server` folder

## 7. Run with Docker (recommended for sharing via Git)

If you plan to share the repository and let someone else run the service using Docker, do the following:

- Add a `.env` file in the `server` folder (do not commit it). Use `.env.example` as a template and send the actual `.env` privately to your friend.
- The project includes a `docker-compose.yml` that builds the `api` image and uses official `postgres` and `redis` images.

From the `server` folder:

```bash
# build image and start services
docker compose build
docker compose up

# or run in background
docker compose up -d --build
```

To stop and remove containers:

```bash
docker compose down
```

Notes:

- Your friend only needs Docker / Docker Desktop installed; they do not need to install Postgres or Redis locally.
- Make sure to provide the `.env` file privately — the `.env.example` file in the repo is safe to commit.
