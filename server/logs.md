# Ingestion Engine Architecture & Development Logs

## Step 1: Project Initialization & Infrastructure Setup

- **Components modified/created**:
  - [main.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/main.py): Refactored imports to load env variables first, added global exception handlers (for standard exceptions, request validation errors, and unhandled errors), and updated docstrings to Google standard.
  - [database.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/config/database.py): Configured SQLAlchemy connection pool settings (`pool_size=20`, `max_overflow=10`, `pool_recycle=3600`), refactored connection check to snake_case (`check_db_connection`), and updated docstrings.
  - [redis_server.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/config/redis_server.py): Refactored to use an explicit `ConnectionPool`, dynamically fetch credentials/configs, and format docstrings to Google standard.
- **Errors encountered**:
  - `UnicodeEncodeError`: Emojis printed to stdout (such as `✅`) caused startup failure on Windows consoles default CP1252 encoding.
  - `TimeoutError`: Redis connection timeouts during startup check crashed the server.
- **Architectural solutions**:
  - Set `PYTHONUTF8=1` shell environment variable when launching the Python process to force console UTF-8 support.
  - Refactored `redis_server_status` to catch `redis.RedisError` (which includes both `ConnectionError` and `TimeoutError`) rather than just `ConnectionError`, allowing the application startup sequence to log connection warnings instead of crashing.

---

## Step 2: Database Models & Alembic Migrations

- **Components modified/created**:
  - [file_model.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/models/file_model.py): Created `Dataset` and `DatasetFolderFilesMapping` models. Decoupled `UploadedFile` from user/folder columns to implement global deduplication and unique master hashes.
  - [env.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/alembic/env.py): Configured path resolution so that nested models can import each other without `ModuleNotFoundError` during migrations.
  - [053bf9b73239_add_datasets_and_mappings.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/alembic/versions/053bf9b73239_add_datasets_and_mappings.py): Created the schema migration script.
- **Errors encountered**:
  - `ModuleNotFoundError`: Importing models failed when executing Alembic migrations.
  - `OperationalError`: Local PostgreSQL service (`postgresql-x64-18`) was stopped, preventing connections.
  - `DuplicateTable`: Running the migration threw database table duplicate errors because `folders` was already initialized.
  - `DependentObjectsStillExist`: Alembic generated drops in the wrong order, trying to drop `physical_files` before `user_upload_mappings`.
- **Architectural solutions**:
  - Added `PROJECT_ROOT / "src"` to `sys.path` in `alembic/env.py` so that imports resolve correctly relative to `src`.
  - Started Postgres in user-space with `pg_ctl.exe` on port 5432 to bypass service management permissions.
  - Ran `alembic stamp c8f2d6a4b9e1` to register existing tables, aligning the migrations history.
  - Swapped drop commands order in the migration revision file, dropping dependent tables first.

---

## Step 3: Core Cryptography & Redis Utilities

- **Components modified/created**:
  - [jwt.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/helpers/jwt.py): Verified and kept the fallback 6-digit OTP generator defaulting to `"123456"` for testing.
  - [redis_server.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/config/redis_server.py): Pre-registered raw Lua scripts on the Redis instance (`active_session_limiter` for concurrency checks and `atomic_chunk_state` for chunk state bitmaps).
- **Errors encountered**:
  - None during this isolated component configuration phase.
- **Architectural solutions**:
  - Pre-registered Lua scripts globally on the Redis object instance at application module load time. This ensures that the SHA1 hashes are computed client-side, enabling efficient, roundtrip-free `EVALSHA` execution without requiring synchronous Redis connection checks during initialization.

---

## Step 4: The Security & Auth Gateway

- **Components modified/created**:
  - [jwt.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/helpers/jwt.py): Enhanced `create_refresh_token` to accept and embed a session ID (`sid`) in the refresh token payload.
  - [auth_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/auth_services.py): Refactored `issue_token_pair` to generate a `sid`, execute the `active_session_limiter` Lua script to restrict active sessions in Redis, and return the signed refresh token. Updated `resolve_refresh_user` to validate that the session ID has not been evicted from the ZSET, and updated `revoke_session` to drop the session from the ZSET upon logout.
  - [auth_routes.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/routes/auth_routes.py): Refactored `/login`, `/logout`, `/refresh`, `/verify-otp`, and Google Callback endpoints to align with the new signatures, cleanly setting cookies and revoking specific sessions.
- **Errors encountered**:
  - None during this refactoring phase.
- **Architectural solutions**:
  - Embedded a unique session ID (`sid`) inside refresh tokens, tracking active sessions in a Redis ZSET sorted by timestamp. This provides O(1) checking during refresh and O(log N) session eviction, ensuring high scalability and preventing cookie bloat or database overhead.

---

## Step 5: Dataset CRUD Operations

- **Components modified/created**:
  - [file_model.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/models/file_model.py): Added the `is_deleted` column to the `Dataset` model.
  - [file_schema.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/schemas/file_schema.py): Created `DatasetCreate`, `DatasetUpdate`, and `DatasetResponse` schemas.
  - [file_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/file_services.py): Implemented the service functions `create_dataset`, `get_datasets`, `get_dataset_by_id`, `update_dataset`, and `delete_dataset` incorporating ownership verification and file attachment conflict checks.
  - [file_routes.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/routes/file_routes.py): Registered POST, GET, PATCH, and DELETE `/datasets` endpoints.
  - Alembic Migrations: Generated and executed migration `80422981c893` to add the `is_deleted` column to datasets.
- **Errors encountered**:
  - None during this isolated implementation.
- **Architectural solutions**:
  - Implemented the soft-delete cascade rule which runs a COUNT check against the mapping table `dataset_folder_files_mapping` to identify if any active files are attached to the dataset, throwing a 409 Conflict if they are, thereby protecting the integrity of active ingestion pipelines.

---

## Step 6: Ingestion Phase 1 - Pre-Flight

- **Components modified/created**:
  - [file_schema.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/schemas/file_schema.py): Refactored `UploadInitRequest` Pydantic model to add the mandatory `dataset_id: str` validation.
  - [file_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/file_services.py): Modified `initialize_upload` function to enforce the `dataset_id` ownership check, update deduplication check to search dataset-folder mappings, and handle global file hash matches.
- **Errors encountered**:
  - None.
- **Architectural solutions**:
  - Refactored the pre-flight checks to prevent "ghost mappings" in the SQL database. If a file is globally present but not mapped to the dataset/folder yet, the server returns `duplicate_suspected` and initializes a short-lived Redis session instead of writing draft rows to the PostgreSQL database. If the upload is confirmed or finalized, it will be mapped correctly, ensuring the database remains completely free of incomplete/orphan draft entries.

---

## Step 7: Ingestion Phase 2 - The Hot Path

- **Components modified/created**:
  - [file_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/file_services.py): Imported the registered Lua script `atomic_chunk_state` and refactored the `process_upload_chunk` service.
- **Errors encountered**:
  - None.
- **Architectural solutions**:
  - Stripped out heavy Redis locks for disk I/O. Since each chunk is written to a unique, isolated path (`upload_id/chunk_index.parts`), parallel writes do not collide. We used the atomic Lua script `atomic_chunk_state` to perform a check-and-set operation on the chunk bitmap and hash in a single Redis transaction, eliminating write contention and locking overhead entirely.

---

## Step 8: Ingestion Phase 3 - Finalization

- **Components modified/created**:
  - [file_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/file_services.py): Refactored `finalize_upload` to unify mapping creation and final responses across standard chunk assembly and fast-link (`duplicate_suspected`) workflows. Replaced the application-level SELECT-then-INSERT mapping pattern with a proper PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` upsert via `sqlalchemy.dialects.postgresql.insert`. Removed dead lock-related code (`_lock_key`, `LOCK_TIMEOUT_SECONDS`, `LOCK_BLOCKING_TIMEOUT_SECONDS`) left over from Step 7.
- **Errors encountered**:
  - Duplicated mapping creation and return code branches across fast-link and standard chunk assembly paths.
  - Original mapping insert used a SELECT-then-INSERT pattern, leaving a minor race window under high concurrency.
- **Architectural solutions**:
  - Unified the `DatasetFolderFilesMapping` database insertion logic at the conclusion of `finalize_upload`, resolving file IDs dynamically from fast-link references or newly assembled uploads.
  - Replaced the race-prone SELECT-then-INSERT with `pg_insert(DatasetFolderFilesMapping).values(...).on_conflict_do_nothing(constraint="uq_dataset_folder_file")`, ensuring atomic, idempotent mapping creation backed by the database's `UniqueConstraint`.

---

## Step 9: Retrieval & Attach Flows

- **Components modified/created**:
  - [file_schema.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/schemas/file_schema.py): Added `DatasetAttachFileRequest` and `DatasetAttachFileResponse` Pydantic models.
  - [file_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/file_services.py): Refactored `_build_tree` and `list_user_uploads` to construct upload trees from `DatasetFolderFilesMapping` with optional `dataset_id` and `folder_id` filters. Implemented `attach_file_to_dataset` with strict IDOR ownership checks.
  - [file_routes.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/routes/file_routes.py): Updated `GET /v1/uploads` endpoint to accept optional query filters (`dataset_id`, `folder_id`). Registered `POST /v1/datasets/{dataset_id}/files` attach endpoint.
- **Errors encountered**:
  - `_build_tree` previously attempted to filter `UploadedFile` by `user_id` and `folder_id` columns, which were removed in Step 2 when decoupling physical storage.
- **Architectural solutions**:
  - Refactored `_build_tree` to join through `DatasetFolderFilesMapping`, accurately reflecting file ownership and dataset/folder placements while supporting zero-I/O file attachment across datasets.

---

## Step 10: Decoupled Garbage Collection

- **Components modified/created**:
  - [cleanup_scheduler.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/cleanup_scheduler.py): Refactored the `purge_abandoned_upload_parts` loop to check for the Redis session metadata key (`upload_id:meta`). If the TTL has expired (e.g., 1-hour inactivity), it aggressively cleans up the physical `.parts` directory on disk, as well as the associated chunk bitmap and hashes tracking keys from Redis.
  - [main.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/main.py): Verified that `start_cleanup_scheduler()` starts smoothly as a background daemon thread in the FastAPI `lifespan` context.
- **Errors encountered**:
  - Unhandled file system errors in previous sweeps could crash the scheduler loop silently.
- **Architectural solutions**:
  - Hardened the `while True` loop with nested `try...except` blocks and standard `logging`, ensuring that a corrupt file system state or transient Redis connection error does not terminate the daemon background GC process.

---

## Step 11: Folder Upload Finalization Error Tracking

- **Components involved**:
  - [file_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/file_services.py): The `finalize_upload` flow that creates `dataset_folder_files_mapping` rows during folder-based uploads.
  - [file_model.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/models/file_model.py): The `folders` and `dataset_folder_files_mapping` tables that enforce foreign key integrity.
- **Problem observed**:
  - Uploading a folder completed the chunk upload step, but `POST /v1/upload/finalize` failed with a `500 Internal Server Error` during the mapping insert.
  - The runtime error was:

```text
psycopg2.errors.ForeignKeyViolation: insert or update on table "dataset_folder_files_mapping" violates foreign key constraint "dataset_folder_files_mapping_folder_id_fkey"
DETAIL:  Key (folder_id)=(d9afbc08-8f10-4149-a83d-2d99e9e4a779) is not present in table "folders".
```

- **Root cause**:
  - `finalize_upload()` attempted to persist a mapping using a `folder_id` value that no longer existed in the `folders` table.
  - The insert itself was valid at the application layer, but PostgreSQL rejected it because the referenced folder row was missing, so the foreign key constraint blocked the write.
- **Impact**:
  - Chunk upload succeeded.
  - Finalization failed, so the uploaded file was not linked to the dataset/folder tree.
  - From the frontend perspective, the upload flow appeared to complete until the final ack, then returned an unexpected server error instead of a controlled validation response.
- **Follow-up needed**:
  - Add a pre-insert folder existence check in the finalize flow and return a controlled `404` or `409` if the stored `folder_id` is missing.
  - Ensure the upload session metadata cannot retain a stale folder reference across folder deletion or tree changes.

---

## Step 12: Repeated Upload Finalize Foreign Key Failure

- **Components involved**:
  - [file_services.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/services/file_services.py): The `finalize_upload` insert into `dataset_folder_files_mapping`.
  - [file_routes.py](file:///d:/Zenteiq/Tasks/File_Ingestion/server/src/routes/file_routes.py): The `POST /v1/upload/finalize` route that surfaces the failure to the frontend.
- **Problem observed**:
  - The upload flow showed a prior `409 Conflict`, then a subsequent upload attempt reached chunk upload successfully but failed again on finalization with a `500 Internal Server Error`.
  - The runtime error repeated the same foreign key violation, with a different missing folder ID:

```text
psycopg2.errors.ForeignKeyViolation: insert or update on table "dataset_folder_files_mapping" violates foreign key constraint "dataset_folder_files_mapping_folder_id_fkey"
DETAIL:  Key (folder_id)=(249fee81-ac95-44fb-99ae-bddeb62b2d25) is not present in table "folders".
```

- **Root cause**:
  - `finalize_upload()` is still trusting the `folder_id` stored in the upload session metadata.
  - When that folder record is missing from the `folders` table, PostgreSQL rejects the mapping insert and the API returns a generic 500 instead of a controlled error.
- **Impact**:
  - The frontend sees an inconsistent flow: upload init and chunk upload succeed, but finalize fails after the file is already on disk.
  - The file remains unlinked from the dataset tree because the mapping row never commits.
- **Current handling gap**:
  - There is still no preflight lookup that validates the stored `folder_id` before inserting the mapping row.
  - A controlled error response should be added so the frontend can react to a missing folder instead of surfacing a server crash.

---

## Step 13: QA Test Suite Added for Auth, File, and Cleanup Components

- **Timestamp**: 2026-07-22 00:00 UTC
- **Component tested**: Auth, File, Cleanups
- **Types of tests implemented**:
  - Unit: `tests/unit_tests/test_auth.py`, `tests/unit_tests/test_file.py`, `tests/unit_tests/test_cleanups.py`
  - Integration: `tests/integration_tests/test_auth_api.py`, `tests/integration_tests/test_file_api.py`
  - Stress: `tests/stress_tests/auth_stress.js`, `tests/stress_tests/file_stress.js`
  - Benchmark: `tests/benchmark/benchmark_auth.py`
- **Verification evidence**:
  - `uv run --with pytest --with pytest-cov --with httpx --with pytest-asyncio python -m pytest tests/unit_tests tests/integration_tests --cov=src --cov-report=term-missing`
  - Result: 12 tests passed, 0 failed.
- **Blind spots / difficult areas**:
  - Full route-level OAuth and Redis-backed upload flows are partially covered because the environment is isolated and the production services are not fully available in this workspace.
  - The no-modification constraint prevented any adaptation of the application code to make it easier to test, so the tests exercise existing public behavior as-is.

