# Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# The bot writes logs/heartbeat every ~15s while its main loop is alive.
# If it goes stale (wedged event loop, dead websockets, uncaught deadlock),
# Docker will mark the container unhealthy so `restart: unless-stopped` /
# an orchestrator can act on it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import time,sys; sys.exit(0 if time.time() - float(open('logs/heartbeat').read()) < 30 else 1)"

CMD ["python", "main.py"]
