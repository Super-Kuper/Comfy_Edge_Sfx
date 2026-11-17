# =============================================================================
# Audio serverless worker — RunPod serverless (contract audio-serverless/v1).
#
# Base image — the official ComfyUI v0.44.0 CUDA 13.0 image, pinned by digest
# (the same base the image keyframe worker uses). It is a pod image: /start.sh
# materializes ComfyUI at pod boot. A serverless worker needs a materialized
# layout and its own boot sequence: docker/install-comfy-packages.sh кладёт
# ComfyUI и venv в образ, docker/entrypoint.sh поднимает ComfyUI на loopback,
# докачивает pinned-веса на том (VOLUME_AUTOPOPULATE=true) и запускает handler.
#
# Structural build without ComfyUI/GPU (checks package/handler wiring only; not
# meant to run):
#   docker build --build-arg BASE_IMAGE=python:3.12-slim -t audio-serverless:stub .
#   docker run --rm --entrypoint python audio-serverless:stub -c "import audio_worker"
#
# Boundaries:
#   - no secrets, tokens or model weights in the image: S3_* come from the
#     endpoint env, weights from the network volume at /runpod-volume;
#   - ComfyUI listens on loopback only and is never exposed: no EXPOSE on purpose;
#   - model files are downloaded to the volume by populate-volume.sh
#     (models-manifest.txt, pinned URL/revision/sha256).
# =============================================================================

ARG BASE_IMAGE=runpod/comfyui:2.2.0-comfyuiv0.44.0-cuda13.0@sha256:adccffa4dc2151b48dd7110b7b03a5f9450b059bc8524bc493b93e5155aea3e2

FROM ${BASE_IMAGE}

ARG AUDIO_WORKER_VERSION=0.2.6

# ComfyUI материализуется на этапе сборки из baked-раскладки базового образа.
# VOLUME_AUTOPOPULATE=true — автоскачивание pinned-весов на том при старте
# (entrypoint); false — только ручной populate-volume.sh.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COMFYUI_DIR=/workspace/runpod-slim/ComfyUI \
    COMFYUI_PYTHON=/workspace/runpod-slim/.venv-cu130/bin/python \
    COMFYUI_BAKED_DIR=/opt/comfyui-baked \
    COMFY_HOST=127.0.0.1 \
    COMFY_PORT=8188 \
    COMFY_URL=http://127.0.0.1:8188 \
    COMFY_BOOT_TIMEOUT_SECONDS=600 \
    VOLUME_ROOT=/runpod-volume \
    MODELS_ROOT=/runpod-volume/models \
    MODELS_MANIFEST=/opt/audio-worker/models-manifest.txt \
    VOLUME_AUTOPOPULATE=true \
    AUDIO_WORKER_VERSION=${AUDIO_WORKER_VERSION} \
    PYTHONPATH=/opt/audio-worker/src

USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates rsync git; \
    rm -rf /var/lib/apt/lists/*

# Worker Python dependencies (runpod SDK, httpx for ComfyUI, jsonschema for the
# contract, boto3 for large audio_url results). The ComfyUI base ships its own
# torch/CUDA stack: never reinstall it here.
RUN set -eux; \
    py="$(command -v python3 || command -v python)"; \
    echo "worker deps python: ${py}"; \
    "${py}" -m pip install --no-cache-dir "runpod==2.2.1" "httpx>=0.27,<0.29" "jsonschema>=4.21" "boto3>=1.35"; \
    "${py}" -c "import boto3, httpx, jsonschema, runpod; print('worker deps ok')"

# ComfyUI v0.44.0: раскладка материализуется из baked-копии базового образа
# (rsync + venv --system-site-packages); stub-сборка без baked-копии шаг пропускает.
COPY docker/install-comfy-packages.sh /opt/audio-worker/install-comfy-packages.sh
RUN bash /opt/audio-worker/install-comfy-packages.sh

# Hub test pod и RunPod runtime запускают команду контейнера через shell, а в
# pod-базе runpod/comfyui имени `python` в PATH нет: ComfyUI живёт в
# /workspace/runpod-slim/.venv-cu130 и venv не активирован (есть только
# python3). Симлинк на тот же интерпретатор, куда поставлены зависимости
# воркера, делает ENTRYPOINT `python handler.py` резолвимым; на python-slim
# (stub-сборка) `python` уже есть и шаг пропускается.
RUN set -eux; \
    if ! command -v python >/dev/null 2>&1; then \
        py="$(command -v python3 || command -v python)"; \
        echo "python -> ${py}"; \
        ln -sfn "${py}" /usr/local/bin/python; \
    fi; \
    python -V

WORKDIR /opt/audio-worker

COPY src/ /opt/audio-worker/src/
COPY handler.py /opt/audio-worker/handler.py
COPY contract/ /opt/audio-worker/contract/
COPY models-manifest.txt /opt/audio-worker/models-manifest.txt
COPY populate-volume.sh /opt/audio-worker/populate-volume.sh
COPY scripts/wait-comfy.sh /opt/audio-worker/wait-comfy.sh
COPY docker/entrypoint.sh /opt/audio-worker/entrypoint.sh
COPY docker/extra_model_paths.yaml /opt/audio-worker/extra_model_paths.yaml

RUN chmod 0755 \
    /opt/audio-worker/populate-volume.sh \
    /opt/audio-worker/wait-comfy.sh \
    /opt/audio-worker/entrypoint.sh

# Структурная проверка: пакет и handler импортируются тем же `python`, которым
# запускается контейнер (ловит отсутствие интерпретатора/зависимостей до Hub).
RUN python -c "import audio_worker.handler; print('audio_worker.handler import ok')"

# Entrypoint: pinned-веса на том → ComfyUI на 127.0.0.1:8188 → /system_stats →
# handler. Наружу ComfyUI не публикуется (EXPOSE нет).
ENTRYPOINT ["/opt/audio-worker/entrypoint.sh"]
