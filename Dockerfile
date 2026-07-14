ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system imagegen && adduser --system --ingroup imagegen imagegen

COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY alembic.ini app.py run_worker.py ./
COPY migrations ./migrations
COPY imagegen ./imagegen
COPY static ./static
COPY templates ./templates
COPY config ./config

RUN mkdir -p /data && chown -R imagegen:imagegen /app /data
USER imagegen

EXPOSE 7860

CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "700", "app:app"]
