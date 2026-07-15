
import uvicorn
from config.database import Check_db_Connection
from helpers.getenv import load_environment_variables
from config.redis_server import redis_server_status
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def Health_Check():
    """
    Health check endpoint to verify the status of the application.

    Returns:
        dict: A dictionary containing the status of the application.
    """
    return {"status": "Application is running successfully!"}


def main():

    # Checking the DOTENV File Status
    load_environment_variables()

    # Checking the Redis Server Status
    redis_server_status()

    # Checking the Database Connection Status
    Check_db_Connection()

    # Start the Uvicorn server with the specified host, port, and reload options
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_level="info")


if __name__ == "__main__":
    main() # Call the startup event function to perform checks on application startup
