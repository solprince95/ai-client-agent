FROM python:3.11-slim

WORKDIR /app

# System deps needed by some of our Python packages (cryptography, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects PORT (usually 8080) at runtime; gunicorn must bind to it.
ENV PORT=8080
EXPOSE 8080

# Same worker layout as the Procfile used on Render.
CMD exec gunicorn app:app \
    --bind 0.0.0.0:${PORT} \
    --timeout 120 \
    --worker-class gthread \
    --workers 1 \
    --threads 8 \
    --worker-tmp-dir /dev/shm
