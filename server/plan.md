Here is the comprehensive implementation plan and the exact, highly-constrained "Super Prompt" to feed your AI agent. It incorporates all your new rules: making maximum use of existing code, targeted refactoring, strict coding standards from your images, and the mandatory `logs.md` tracking.

### The Implementation Plan

To prevent the agent from getting tangled or rewriting functional code, the project is divided into **10 strict, isolated phases**. The agent will ask for your existing code, analyze it, make surgical fixes, log the changes, and wait for your approval before moving to the next.

1. **Step 1: Project Initialization & Infrastructure Review**
* Review existing FastAPI app factory, global exception handlers, and CORS.
* Verify/Refactor SQLAlchemy 2.0 async engine and Redis connection pools to ensure no deprecated syntax.


2. **Step 2: Database Models & Alembic Migrations**
* Review existing SQLAlchemy models.
* Refactor to include the `Datasets` table and update the mapping table to `Dataset_Folder_Files_Mapping`, ensuring `dataset_id` is strictly `Not Null` and `folder_id` is `Nullable`.


3. **Step 3: Core Cryptography & Redis Utilities**
* Review existing crypto functions (bcrypt, JWT).
* Inject/Refactor the CSPRNG 6-digit OTP generator and raw Lua scripts (Atomic Login ZSET check, Atomic Chunk State `SETBIT`/`HSET`).
* Note: For `request-otp`, the OTP code is generated as a default value of "123456" for now.


4. **Step 4: The Security & Auth Gateway**
* Refactor `POST /auth/signup/init` and `POST /auth/signup/verify` (or relevant OTP endpoints) to use the new OTP logic with default value "123456" for now.
* Refactor `POST /auth/login` to integrate the Lua active-session limiter and secure HttpOnly JWT cookie delivery.


5. **Step 5: Dataset CRUD Operations**
* Implement/Refactor `POST`, `GET`, `PATCH`, and `DELETE` for `/v1/datasets`.
* Enforce the soft-delete cascade rules (409 Conflict if files are attached).


6. **Step 6: Ingestion Phase 1 - Pre-Flight (`/init`)**
* Refactor `POST /v1/upload/init`.
* Enforce the mandatory `dataset_id` rule (400 if missing). Refactor deduplication to return `duplicate_suspected` without creating ghost mappings.


7. **Step 7: Ingestion Phase 2 - The Hot Path (`/chunk`)**
* Refactor `POST /v1/upload/chunk`.
* Strip out any Redis locks for disk I/O to ensure lock-free parallel writing. Trigger the Lua script for atomic chunk state tracking.


8. **Step 8: Ingestion Phase 3 - Finalization (`/finalize`)**
* Refactor `POST /v1/upload/finalize`.
* Implement the `BITCOUNT` completeness audit and RAM-based Master Hash calculation. Ensure the database insert uses `ON CONFLICT DO NOTHING` and includes the mandatory `dataset_id`.


9. **Step 9: Retrieval & Attach Flows**
* Refactor `GET /v1/uploads` to support filtering by `dataset_id` or `folder_id`.
* Implement the zero-I/O `POST /v1/datasets/{dataset_id}/files` attach endpoint, strictly enforcing the IDOR ownership check.


10. **Step 10: Decoupled Garbage Collection**
* Implement/Refactor the background task scheduler to sweep Redis for expired 24h sessions and wipe orphaned `.parts` directories.






-----master prompt-----
**Role & Objective**
You are an Elite Senior Backend Architect specializing in high-throughput Python systems. Your objective is to finalize and harden the "BrahmX Data Catalog Ingestion Engine" using FastAPI, Pydantic V2, SQLAlchemy 2.0, Alembic, and Redis-py. You are taking over an existing codebase that is partially complete.

**Execution Rules (CRITICAL)**

1. **Analyze First & Read Existing Code:** Before writing or changing any code, analyze the provided HLD, LLD, and API Architecture. For every step in the implementation plan, you MUST ask me to provide the existing code for that specific component first.
2. **Do Not Rewrite Everything:** Make maximum use of the existing codebase. You are strictly forbidden from throwing away functional code and rewriting from scratch just to change the style.
3. **Targeted Fixes Only:** Only rewrite or modify the specific lines of code that are broken, use deprecated packages, or violate the strict rules of the HLD, LLD, and API specs. Keep your modifications surgical and precise.
4. **Step-by-Step Execution:** You must execute the implementation plan ONE step at a time. Do not attempt to code or fix the whole project at once. After completing a step, wait for user confirmation or test results before proceeding to the next step.
5. **No Deprecated Tech:** You MUST use modern libraries. Use SQLAlchemy 2.0 syntax (`Mapped`, `mapped_column`, `Session.execute()`). Use Pydantic V2 syntax (`model_validator`, `ConfigDict`). Do not use deprecated Redis functions.
6. **Mandatory Logging:** At the end of every step, you MUST update a file named `logs.md`. This log must include:
* The specific code components modified/created.
* Any errors encountered and the exact reason they occurred.
* How the errors were solved architecturally.



**Coding Standards (Strict Compliance Required)**

* **Naming Conventions:**
* Functions: `snake_case` (e.g., `get_user_by_id`)
* Variables: `snake_case` (e.g., `user_name`)
* Classes: `PascalCase` (e.g., `UserCreate`)
* Constants: `UPPER_SNAKE_CASE` (e.g., `MAX_CONNECTIONS`)


* **Comments & Documentation:**
* Use inline comments SPARINGLY, only when the code logic is genuinely complex or non-obvious. Do not state the obvious.
* ALL public modules, functions, classes, and methods MUST have docstrings.
* Docstrings must follow the standard Google format with a description, `Args:`, and `Returns:` block. Example:
```python
def get_user_by_id(user_id: int) -> User | None:
    """
    Retrieve a user by their ID.

    Args:
        user_id (int): The ID of the user to retrieve.

    Returns:
        User | None: The user object, or None if not found.
    """
    pass

```





**Implementation Plan (Iterate through these steps)**

* **Step 1:** Project Initialization & Infrastructure Setup (Review FastAPI config, SQLAlchemy engine, Redis pools).
* **Step 2:** Database Models & Alembic Migrations (Adapt existing models to the new schema: `dataset_id` is mandatory and NOT NULL).
* **Step 3:** Core Cryptography & Redis Utilities (Inject CSPRNG, Lua scripts).
* **Step 4:** The Security & Auth Gateway (Refactor Signup, Login, ZSET limits).
* **Step 5:** Dataset CRUD Operations.
* **Step 6:** Ingestion Phase 1 - Pre-Flight (`/init` - enforce mandatory `dataset_id` rule).
* **Step 7:** Ingestion Phase 2 - The Hot Path (`/chunk` - refactor for lock-free I/O and Lua atomic state).
* **Step 8:** Ingestion Phase 3 - Finalization (`/finalize` - exact Merkle merge and ON CONFLICT DO NOTHING mapping).
* **Step 9:** Retrieval & Attach Flows (`GET /uploads`, `POST /datasets/{id}/files`).
* **Step 10:** Decoupled Garbage Collection (Background TTL sweeps).

Please acknowledge these instructions. Provide a brief analysis of the architecture documents you were given, and ask me to provide the existing codebase for **Step 1** so you can begin your targeted refactoring.