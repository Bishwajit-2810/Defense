# Build from the REPO ROOT:
#   docker build -f services/workers/stage1_nlp/Dockerfile .
# (deploy/docker-compose.yml sets `context: ..` accordingly)
#
# DEFAULT = STUB-MODE IMAGE (MODEL_STUB_MODE=true): installs only
# requirements-stub.txt. For real ML inference, switch the pip install to the
# full requirements.txt (torch/transformers — multiple GB) and add the system
# packages it needs:
#   RUN apt-get update && apt-get install -y libgomp1 tesseract-ocr tesseract-ocr-ben \
#       && rm -rf /var/lib/apt/lists/*
FROM python:3.11-slim
WORKDIR /app
COPY src/defense/services/workers/stage1_nlp/requirements-stub.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY libs /app/libs
COPY src/defense/services/workers/__init__.py /app/services/workers/__init__.py
COPY src/defense/services/workers/stage1_nlp /app/services/workers/stage1_nlp
ENV PYTHONPATH=/app
# __main__.py uses package-relative imports -> run as a module from /app
CMD ["python", "-m", "services.workers.stage1_nlp"]
