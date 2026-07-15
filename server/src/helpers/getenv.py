from dotenv import load_dotenv
from pathlib import Path
import os

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# It Loads the Environment Variables from the When the App Starts
def load_environment_variables():
    """
    Load environment variables from a .env file from the Parent Folder.

    Raises:
        FileNotFoundError: If the .env file is not found at the specified path.
    """
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env file not found at {ENV_PATH}")
    
    if(load_dotenv(ENV_PATH)):
        print(f"✅ Environment Variables Loaded from its Path: {ENV_PATH}")
    else:
        print(f"❌ Failed to Load Environment Variables from its Path: {ENV_PATH}")


def get_env(key: str, default=None, required = True):
    """
    Get the value of an environment variable.

    Args:
        key (str): The name of the environment variable.
        default: The default value to return if the environment variable is not set.
        required (bool): If True, raises an exception if the environment variable is not set.

    Returns:
        The value of the environment variable, or the default value if not set.
    """
    value = os.getenv(key, default)
    if required and value is None:
        raise ValueError(f"Environment variable '{key}' is required but not set.")
    return value