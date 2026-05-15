FROM python:3.13-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY fns_cli/ ./fns_cli/

# Default config mount point
VOLUME ["/app/config", "/app/vault"]

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "fns_cli.main"]
CMD ["run", "-c", "/app/config/config.yaml"]
