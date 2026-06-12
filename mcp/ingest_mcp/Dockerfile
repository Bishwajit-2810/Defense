# Build from the REPO ROOT:
#   docker build -f mcp/ingest_mcp/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY mcp/ingest_mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY mcp/__init__.py /app/mcp/__init__.py
COPY mcp/ingest_mcp /app/mcp/ingest_mcp
ENV PYTHONPATH=/app
# server.py uses cwd-relative imports (`from manifest import ...`),
# so uvicorn must run from the service directory.
WORKDIR /app/mcp/ingest_mcp
EXPOSE 8102
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8102"]
