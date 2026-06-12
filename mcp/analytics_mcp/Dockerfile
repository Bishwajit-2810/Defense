# Build from the REPO ROOT:
#   docker build -f mcp/analytics_mcp/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY mcp/analytics_mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY mcp/__init__.py /app/mcp/__init__.py
COPY mcp/analytics_mcp /app/mcp/analytics_mcp
ENV PYTHONPATH=/app
# server.py uses package-relative imports (`from .manifest import ...`),
# so serve it by its full module path from /app.
EXPOSE 8100
CMD ["uvicorn", "mcp.analytics_mcp.server:app", "--host", "0.0.0.0", "--port", "8100"]
