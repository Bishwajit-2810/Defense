# Build from the REPO ROOT:
#   docker build -f services/api/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY services/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY libs /app/libs
COPY services/api /app/services/api
ENV PYTHONPATH=/app
# main.py uses cwd-relative imports (`from routers import ...`, `from deps
# import ...`), so uvicorn must run from the service directory.
WORKDIR /app/services/api
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
