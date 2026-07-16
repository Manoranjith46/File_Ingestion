
"""Application entrypoint for the FastAPI server."""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config.DataBase import Check_db_Connection, get_engine
from config.RedisServer import redis_server_status
from helpers.GetEnv import load_environment_variables
from models.auth_model import Base
from routes.auth_routes import auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
        Load environment settings,Create Redis-Server and prepare the database during application startup.
    """
    load_environment_variables()
    redis_server_status()
    Check_db_Connection()
    Base.metadata.create_all(bind=get_engine())
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])


@app.get("/")
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
