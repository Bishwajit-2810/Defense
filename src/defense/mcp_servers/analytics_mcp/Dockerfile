# Build from the REPO ROOT:
#   docker build -f mcp_servers/analytics_mcp/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY mcp_servers/analytics_mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY mcp_servers/__init__.py /app/mcp_servers/__init__.py
COPY mcp_servers/analytics_mcp /app/mcp_servers/analytics_mcp
ENV PYTHONPATH=/app
# FastMCP server served by full module path from /app; exposes the
# streamable-HTTP MCP endpoint at /mcp.
EXPOSE 8100
CMD ["uvicorn", "mcp_servers.analytics_mcp.server:app", "--host", "0.0.0.0", "--port", "8100"]
