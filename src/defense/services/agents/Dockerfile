# Build from the REPO ROOT:
#   docker build -f services/agents/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY src/defense/services/agents/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY libs /app/libs
COPY src/defense/services/agents /app/services/agents
ENV PYTHONPATH=/app
# main.py uses package-relative imports (`from .registry import ...`),
# so serve it by its full module path from /app.
EXPOSE 8010
CMD ["uvicorn", "services.agents.main:app", "--host", "0.0.0.0", "--port", "8010"]
