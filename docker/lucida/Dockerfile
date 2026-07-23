ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface \
    TRANSFORMERS_CACHE=/root/.cache/huggingface \
    PIP_DEFAULT_TIMEOUT=180 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Default: CUDA torch (cu124). CPU fallback:
#   --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --upgrade pip \
 && pip install torch torchvision --index-url "${TORCH_INDEX_URL}"

COPY .tmp-lucida-src/lucida-main/pyproject.toml ./
COPY .tmp-lucida-src/lucida-main/bgr/ bgr/
COPY .tmp-lucida-src/lucida-main/benchmark/ benchmark/
RUN pip install .

COPY .tmp-lucida-src/lucida-main/serving/ serving/
COPY docker/lucida/docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/docker-entrypoint.sh"]
