
"""Application entrypoint for the FastAPI server."""

from contextlib import asynccontextmanager
from sys import prefix

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.database import Check_db_Connection, get_engine
from config.redis_server import redis_server_status
from helpers.get_env import get_env
from helpers.get_env import load_environment_variables

# Load environment variables before importing modules that depend on them.
load_environment_variables()

from models.auth_model import Base
from models import file_model  # noqa: F401
from services.cleanup_scheduler import start_cleanup_scheduler
from routes.file_routes import file_router
from routes.auth_routes import auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
        Create Redis-Server and prepare the database during application startup.
    """
    redis_server_status()
    Check_db_Connection()
    start_cleanup_scheduler()
    Base.metadata.create_all(bind=get_engine())
    yield


app = FastAPI(lifespan=lifespan)

frontend_url = get_env("FRONTEND_URL", "http://localhost:5173", required=False)
allowed_origins = [origin.strip() for origin in str(frontend_url).split(",") if origin.strip()]

if "http://localhost:5173" not in allowed_origins:
    allowed_origins.append("http://localhost:5173")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Authorization"],
)

app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(file_router, prefix="/v1", tags=["File Ingestion"])


@app.get("/v1/health")
def health_check():
    """
        Return a lightweight health payload for uptime checks.\
    """
    return {"status": "Application is running successfully :)"}


def main():
    """
        Start the Uvicorn development server.
    """
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_level="info")


if __name__ == "__main__":
    main()
