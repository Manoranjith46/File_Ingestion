import redis

server = redis.Redis(
    host='127.0.0.1', 
    port=6379, 
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=5  # Set a timeout for the connection
)

def redis_server_status():
    try:
        server.ping()
        print("✅ Redis Server Running On the localhost Port 6379")
    except redis.ConnectionError:
        print("❌ Failed to connect to Redis Server")

