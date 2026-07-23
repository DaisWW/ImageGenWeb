ARG LUCIDA_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${LUCIDA_BASE_IMAGE}

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

COPY pyproject.toml ./
COPY --from=lucida_bgr . bgr/
COPY --from=lucida_benchmark . benchmark/
RUN pip install .

COPY --from=lucida_serving . serving/
COPY runtime.py lucida_runtime.py

LABEL io.snow-ai-studio.lucida-runtime="uvicorn-preload-v1"

EXPOSE 8000
ENTRYPOINT []
CMD ["uvicorn", "lucida_runtime:app", "--host", "0.0.0.0", "--port", "8000"]
