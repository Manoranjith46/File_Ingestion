"""Redis connection helpers for the FastAPI application."""

import redis

server = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=5,
)


def redis_server_status():
    """
    Check whether Redis is reachable on the configured host and port.

    Returns:
        None: Prints the connection status to stdout.
    """
    try:
        server.ping()
        print("✅ Redis Server Running On the localhost Port 6379")
    except redis.ConnectionError:
        print("❌ Failed to connect to Redis Server")