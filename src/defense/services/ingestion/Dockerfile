# Build from the REPO ROOT:
#   docker build -f services/ingestion/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY src/defense/services/ingestion/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY libs /app/libs
COPY src/defense/services/ingestion /app/services/ingestion
ENV PYTHONPATH=/app
# __main__.py uses package-relative imports -> run as a module from /app
CMD ["python", "-m", "services.ingestion"]
