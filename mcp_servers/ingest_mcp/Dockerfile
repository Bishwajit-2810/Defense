# Build from the REPO ROOT:
#   docker build -f mcp_servers/ingest_mcp/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY mcp_servers/ingest_mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY mcp_servers/__init__.py /app/mcp_servers/__init__.py
COPY mcp_servers/ingest_mcp /app/mcp_servers/ingest_mcp
ENV PYTHONPATH=/app
# Run the FastMCP server from the service directory (server:app); exposes the
# streamable-HTTP MCP endpoint at /mcp.
WORKDIR /app/mcp_servers/ingest_mcp
EXPOSE 8102
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8102"]
