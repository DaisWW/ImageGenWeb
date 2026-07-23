ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface \
    TRANSFORMERS_CACHE=/root/.cache/huggingface \
    PIP_DEFAULT_TIMEOUT=180

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN set -eux; \
    mkdir -p /tmp/wheels; \
    curl -fL --retry 5 --retry-delay 3 -o /tmp/wheels/torch.whl \
      https://download.pytorch.org/whl/cpu/torch-2.13.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl; \
    curl -fL --retry 5 --retry-delay 3 -o /tmp/wheels/torchvision.whl \
      https://download.pytorch.org/whl/cpu/torchvision-0.28.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl; \
    pip install /tmp/wheels/torch.whl /tmp/wheels/torchvision.whl; \
    rm -rf /tmp/wheels

COPY .tmp-lucida-src/lucida-main/pyproject.toml ./
COPY .tmp-lucida-src/lucida-main/bgr/ bgr/
COPY .tmp-lucida-src/lucida-main/benchmark/ benchmark/
RUN pip install .

COPY .tmp-lucida-src/lucida-main/serving/ serving/
COPY docker/lucida/docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/docker-entrypoint.sh"]
