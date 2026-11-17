# =============================================================================
# Audio serverless worker — RunPod serverless (contract audio-serverless/v1).
#
# Base image — the official ComfyUI v0.44.0 CUDA 13.0 image, pinned by digest
# (the same base the image keyframe worker uses). It is a pod image: /start.sh
# materializes ComfyUI at pod boot. A serverless worker needs a materialized
# layout and its own boot sequence, which is a live step (see README "Remaining
# live steps"); this Dockerfile builds the worker layer on top of the pinned base.
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

ARG AUDIO_WORKER_VERSION=0.2.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COMFY_URL=http://127.0.0.1:8188 \
    VOLUME_ROOT=/runpod-volume \
    MODELS_ROOT=/runpod-volume/models \
    MODELS_MANIFEST=/opt/audio-worker/models-manifest.txt \
    PYTHONPATH=/opt/audio-worker/src

USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Worker Python dependencies (runpod SDK, httpx for ComfyUI, jsonschema for the
# contract, boto3 for large audio_url results). The ComfyUI base ships its own
# torch/CUDA stack: never reinstall it here.
RUN set -eux; \
    py="$(command -v python3 || command -v python)"; \
    echo "worker deps python: ${py}"; \
    "${py}" -m pip install --no-cache-dir "runpod==2.2.1" "httpx>=0.27,<0.29" "jsonschema>=4.21" "boto3>=1.35"; \
    "${py}" -c "import boto3, httpx, jsonschema, runpod; print('worker deps ok')"

WORKDIR /opt/audio-worker

COPY src/ /opt/audio-worker/src/
COPY handler.py /opt/audio-worker/handler.py
COPY contract/ /opt/audio-worker/contract/
COPY models-manifest.txt /opt/audio-worker/models-manifest.txt
COPY populate-volume.sh /opt/audio-worker/populate-volume.sh

RUN chmod 0755 /opt/audio-worker/populate-volume.sh

# TODO(live): replace with docker/entrypoint.sh that
#   1. verifies/updates the pinned weights on the volume (populate-volume.sh
#      with VOLUME_AUTOPOPULATE=true);
#   2. boots ComfyUI on 127.0.0.1:8188 with extra_model_paths.yaml pointing at
#      ${MODELS_ROOT} and waits for /system_stats (copy the pattern from
#      image-serverless docker/entrypoint.sh + scripts/wait-comfy.sh);
#   3. then runs this handler. Until that entrypoint exists the image does not
#      boot ComfyUI, so a live endpoint answers with `comfy_timeout`/connection
#      errors — the handler itself is ready.
ENTRYPOINT ["python", "/opt/audio-worker/handler.py"]
