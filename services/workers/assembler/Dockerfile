# Build from the REPO ROOT:
#   docker build -f services/workers/assembler/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
FROM python:3.11-slim
WORKDIR /app
COPY services/workers/assembler/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY libs /app/libs
COPY services/workers/__init__.py /app/services/workers/__init__.py
COPY services/workers/assembler /app/services/workers/assembler
ENV PYTHONPATH=/app
# The assembler uses cwd-relative imports (`from builder import ...`,
# `from assembler import ...`), so run it as a script from its own directory.
WORKDIR /app/services/workers/assembler
CMD ["python", "__main__.py"]
