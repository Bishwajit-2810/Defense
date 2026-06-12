# Build from the REPO ROOT:
#   docker build -f services/workers/stage2_llm/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY services/workers/stage2_llm/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY libs /app/libs
COPY services/workers/__init__.py /app/services/workers/__init__.py
COPY services/workers/stage2_llm /app/services/workers/stage2_llm
ENV PYTHONPATH=/app
# __main__.py uses package-relative imports -> run as a module from /app
CMD ["python", "-m", "services.workers.stage2_llm"]
