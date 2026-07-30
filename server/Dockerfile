FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Install system dependencies commonly needed for DB drivers
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy project files
COPY . .

# Convert Windows CRLF line endings to Unix LF and make executable
RUN sed -i 's/\r$//' ./start.sh && chmod +x ./start.sh

EXPOSE 8000

# Use the repository start script which bootstraps DB then launches uvicorn
CMD ["/app/start.sh"]
