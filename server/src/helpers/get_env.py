"""Environment loading and lookup helpers."""

from pathlib import Path
import os

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_environment_variables():
    """
    Load environment variables from the project .env file.

    Raises:
        FileNotFoundError: If the .env file does not exist.
    """
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env file not found at {ENV_PATH}")

    if load_dotenv(ENV_PATH):
        print(f"✅ Environment Variables Loaded from its Path: {ENV_PATH}")
    else:
        print(f"❌ Failed to Load Environment Variables from its Path: {ENV_PATH}")


def get_env(key: str, default=None, required: bool = True):
    """
    Read an environment variable.

    Args:
        key: The environment variable name.
        default: The fallback value when the variable is missing.
        required: Whether to raise if the variable is missing.

    Returns:
        str | None: The configured environment value or the default.

    Raises:
        ValueError: If the variable is required but not configured.
    """
    value = os.getenv(key, default)
    if required and value is None:
        raise ValueError(f"Environment variable '{key}' is required but not set.")
    return value