# Build from the REPO ROOT:
#   docker build -f mcp/retrieval_mcp/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY mcp/retrieval_mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# server.py imports libs.embeddings via a repo-root sys.path/PYTHONPATH
# fallback, so ship libs/ and keep PYTHONPATH=/app.
COPY libs /app/libs
COPY mcp/__init__.py /app/mcp/__init__.py
COPY mcp/retrieval_mcp /app/mcp/retrieval_mcp
ENV PYTHONPATH=/app
# server.py uses cwd-relative imports (`from manifest import ...`),
# so uvicorn must run from the service directory.
WORKDIR /app/mcp/retrieval_mcp
EXPOSE 8101
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8101"]
