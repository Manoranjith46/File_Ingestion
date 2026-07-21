"""Redis connection helpers for the FastAPI application."""

import redis
from helpers.get_env import get_env

# Load Redis configurations from environment or fallback to local defaults.
redis_host = get_env("REDIS_HOST", "127.0.0.1", required=False)
redis_port = int(get_env("REDIS_PORT", "6379", required=False))

# Create connection pool for performance optimization
redis_pool = redis.ConnectionPool(
    host=redis_host,
    port=redis_port,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=5,
    max_connections=50,
)

server = redis.Redis(connection_pool=redis_pool)


def redis_server_status() -> None:
    """
    Check whether Redis is reachable on the configured host and port.

    Returns:
        None
    """
    try:
        server.ping()
        print(f"✅ Redis Server Running On {redis_host}:{redis_port}")
    except redis.RedisError as e:
        print(f"❌ Failed to connect to Redis Server on {redis_host}:{redis_port}: {e}")


# --- Lua Scripts ---

ACTIVE_SESSION_LIMITER_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local session_id = ARGV[2]
local max_sessions = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
redis.call('ZADD', key, now, session_id)

local card = redis.call('ZCARD', key)
if card > max_sessions then
    local num_to_remove = card - max_sessions
    redis.call('ZREMRANGEBYRANK', key, 0, num_to_remove - 1)
end

redis.call('EXPIRE', key, ttl)
return 1
"""

ATOMIC_CHUNK_STATE_LUA = """
local bitmap_key = KEYS[1]
local hashes_key = KEYS[2]
local meta_key = KEYS[3]
local chunk_index = tonumber(ARGV[1])
local chunk_hash = ARGV[2]
local ttl = tonumber(ARGV[3])

local exists = redis.call('GETBIT', bitmap_key, chunk_index)
if exists == 1 then
    return 1
end

redis.call('SETBIT', bitmap_key, chunk_index, 1)
redis.call('HSET', hashes_key, tostring(chunk_index), chunk_hash)

redis.call('EXPIRE', bitmap_key, ttl)
redis.call('EXPIRE', hashes_key, ttl)
redis.call('EXPIRE', meta_key, ttl)

return 0
"""

# Register Lua scripts on the Redis instance
active_session_limiter = server.register_script(ACTIVE_SESSION_LIMITER_LUA)
atomic_chunk_state = server.register_script(ATOMIC_CHUNK_STATE_LUA)