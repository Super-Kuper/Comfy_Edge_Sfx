#!/usr/bin/env bash
# =============================================================================
# Entrypoint serverless-воркера изображений (контракт audio-serverless/v1).
#
# Порядок работы:
#   1. подготовка тома: LINK_VOLUME_NODES=true → симлинки custom_nodes с
#      /runpod-volume/custom_nodes в ${COMFYUI_DIR}/custom_nodes;
#   2. extra_model_paths.yaml → копия в ${COMFYUI_DIR} (общие модели с тома);
#   3. старт ComfyUI в фоне на 127.0.0.1:8188 (наружу не публикуется!);
#   4. ожидание готовности ComfyUI по /system_stats (scripts/wait-comfy.sh,
#      таймаут COMFY_BOOT_TIMEOUT_SECONDS);
#   5. автоскачивание моделей на том (VOLUME_AUTOPOPULATE=true,
#      populate-volume.sh + models-manifest.txt);
#   6. запуск handler'а: handler.py (PYTHONPATH задан в образе);
#   7. SIGTERM/SIGINT пробрасываются handler'у, ComfyUI гасится при выходе.
#
# Почему не `exec python handler.py`: при exec shell заменяется процессом
# handler'а, и trap на SIGTERM перестал бы работать — ComfyUI оставался бы
# висеть до убийства контейнера. Поэтому handler запускается дочерним
# процессом под присмотром этого скрипта: сигнал уходит handler'у (он может
# завершить текущий job), после чего cleanup останавливает ComfyUI
# (SIGTERM → SIGKILL через 20 с).
#
# База — официальный runpod/comfyui:2.2.0-comfyuiv0.44.0-cuda13.0 по digest, где
#   COMFYUI_DIR=/workspace/runpod-slim/ComfyUI,
#   COMFYUI_PYTHON=/workspace/runpod-slim/.venv-cu130/bin/python.
# Интерпретатор ComfyUI ищется по COMFYUI_PYTHON, затем в типовых местах
# (${COMFYUI_DIR}/{venv,.venv}, ${COMFYUI_DIR}/../.venv-cu130, /opt/venv);
# handler запускается тем же интерпретатором (в нём стоят runpod/httpx/boto3),
# если AUDIO_WORKER_PYTHON не задан явно.
#
# Stub-сборка: на python:3.12-slim ComfyUI нет, скрипт завершается понятной
# ошибкой про отсутствие ComfyUI — это ожидаемо, stub-образ не предназначен
# для реального запуска.
# =============================================================================

set -euo pipefail

# --- Конфигурация (env endpoint'а RunPod; умолчания — src/audio_worker/config.py) ---
COMFYUI_DIR="${COMFYUI_DIR:-/workspace/runpod-slim/ComfyUI}"
COMFY_HOST="${COMFY_HOST:-127.0.0.1}"
COMFY_PORT="${COMFY_PORT:-8188}"
COMFY_BOOT_TIMEOUT_SECONDS="${COMFY_BOOT_TIMEOUT_SECONDS:-600}"
# COMFYUI_PYTHON — интерпретатор venv официального базового образа (там torch
# и pip-зависимости ComfyUI); в Dockerfile задан под layout runpod/comfyui.
# Пустое/несуществующее значение — не ошибка: сработает автопоиск ниже.
COMFYUI_PYTHON="${COMFYUI_PYTHON:-}"
# COMFY_START_CMD — необязательная полная команда старта ComfyUI; нужна, если
# в базовом образе нестандартная раскладка (иначе — venv python + main.py).
COMFY_START_CMD="${COMFY_START_CMD:-}"
# COMFY_KEEP_EXTRA=1 — не перезаписывать extra_model_paths.yaml базового образа.
COMFY_KEEP_EXTRA="${COMFY_KEEP_EXTRA:-0}"
# Том RunPod: layout v1 — /runpod-volume/models, /runpod-volume/custom_nodes.
VOLUME_ROOT="${VOLUME_ROOT:-/runpod-volume}"
# MODELS_ROOT — куда populate-volume.sh и ComfyUI extra paths кладут веса.
MODELS_ROOT="${MODELS_ROOT:-${VOLUME_ROOT}/models}"
# LINK_VOLUME_NODES=true — подключать custom_nodes с тома.
LINK_VOLUME_NODES="${LINK_VOLUME_NODES:-false}"
# VOLUME_AUTOPOPULATE=true — докачать отсутствующие модели на том при старте.
VOLUME_AUTOPOPULATE="${VOLUME_AUTOPOPULATE:-true}"
# HF_TOKEN — токен Hugging Face для gated-репозиториев в models-manifest.txt.
HF_TOKEN="${HF_TOKEN:-}"
AUDIO_WORKER_DIR="${AUDIO_WORKER_DIR:-/opt/audio-worker}"
WAIT_COMFY_SH="${WAIT_COMFY_SH:-${AUDIO_WORKER_DIR}/wait-comfy.sh}"
POPULATE_SH="${POPULATE_SH:-${AUDIO_WORKER_DIR}/populate-volume.sh}"
MANIFEST="${MANIFEST:-${MODELS_MANIFEST:-${AUDIO_WORKER_DIR}/models-manifest.txt}}"
# Интерпретатор handler'а; по умолчанию python из venv ComfyUI.
AUDIO_WORKER_PYTHON="${AUDIO_WORKER_PYTHON:-}"
AUDIO_WORKER_VERSION="${AUDIO_WORKER_VERSION:-unknown}"

# COMFY_URL — единый URL ComfyUI для handler'а (src/audio_worker/config.py).
export COMFY_URL="${COMFY_URL:-http://${COMFY_HOST}:${COMFY_PORT}}"

COMFY_PID=""
HANDLER_PID=""

log() { printf '[entrypoint] %s\n' "$*"; }
warn() { printf '[entrypoint] WARN: %s\n' "$*" >&2; }
die() {
    printf '[entrypoint] ERROR: %s\n' "$*" >&2
    exit 1
}

# --- Остановка дочерних процессов и проброс сигналов -------------------------

# _stop_process PID ИМЯ [GRACE_SECONDS] — SIGTERM, ожидание, затем SIGKILL.
_stop_process() {
    local pid="$1"
    local name="$2"
    local grace="${3:-20}"
    local i
    if [[ -z "${pid}" ]]; then
        return 0
    fi
    if ! kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi
    log "останавливаю ${name} (pid ${pid})"
    kill -TERM "${pid}" 2>/dev/null || true
    for ((i = 0; i < grace * 2; i++)); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    warn "${name} не завершился за ${grace}s — SIGKILL (pid ${pid})"
    kill -KILL "${pid}" 2>/dev/null || true
    return 0
}

_cleanup() {
    local code=$?
    trap - EXIT
    set +e
    # Сначала handler: он может успеть завершить/отменить текущий job.
    _stop_process "${HANDLER_PID}" "handler"
    # ComfyUI держим только пока жив воркер (наружу не публикуется).
    _stop_process "${COMFY_PID}" "ComfyUI"
    exit "${code}"
}
trap _cleanup EXIT

_forward_signal() {
    local signal="$1"
    log "получен SIG${signal}: пробрасываю handler'у (pid ${HANDLER_PID:-нет})"
    if [[ -n "${HANDLER_PID}" ]]; then
        kill -s "${signal}" "${HANDLER_PID}" 2>/dev/null || true
    fi
}
trap '_forward_signal TERM' TERM
trap '_forward_signal INT' INT

# --- 1. Ноды с тома (LINK_VOLUME_NODES) --------------------------------------

# Семантика: запечённые в образ ноды НЕ перезаписываются. Если нода с тем же
# именем уже есть в ${COMFYUI_DIR}/custom_nodes (файл/каталог/симлинк на другой
# путь), версия с тома игнорируется с WARN — приоритет у образа, конфликт
# виден в логах. Недостающие ноды подключаются симлинком на том (не копией):
# так обновление нод на томе подхватывается без пересборки образа.
_prepare_volume_nodes() {
    local src="${VOLUME_ROOT}/custom_nodes"
    local dst="${COMFYUI_DIR}/custom_nodes"
    local entry name target linked=0 kept=0

    if [[ "${LINK_VOLUME_NODES}" != "true" ]]; then
        log "LINK_VOLUME_NODES=${LINK_VOLUME_NODES}: custom_nodes с тома не подключаю"
        return 0
    fi

    if [[ ! -d "${src}" ]]; then
        die "LINK_VOLUME_NODES=true, но нет каталога ${src}: проверьте mount тома"
    fi

    mkdir -p "${dst}"
    for entry in "${src}"/*; do
        if [[ ! -e "${entry}" ]]; then
            continue # пустой каталог custom_nodes на томе
        fi
        name="$(basename "${entry}")"
        target="${dst}/${name}"
        if [[ -L "${target}" ]]; then
            if [[ "$(readlink -f "${target}")" == "$(readlink -f "${entry}")" ]]; then
                log "custom_nodes/${name}: симлинк на том уже подключён"
                continue
            fi
            die "custom_nodes/${name}: в образе симлинк на другой путь ($(readlink -f "${target}")), ожидался $(readlink -f "${entry}")"
        fi
        if [[ -e "${target}" ]]; then
            warn "custom_nodes/${name}: уже есть в образе (запечённая нода) — версия с тома не подключается"
            kept=$((kept + 1))
            continue
        fi
        ln -s "${entry}" "${target}"
        log "custom_nodes/${name}: подключён с тома (symlink)"
        linked=$((linked + 1))
    done
    log "custom_nodes с тома: подключено ${linked}, сохранено запечённых ${kept}"
}

# --- 2. extra_model_paths.yaml ------------------------------------------------

# По умолчанию НАШ файл авторитетен: копируется в ${COMFYUI_DIR} всегда, чтобы
# модели с тома были видны ComfyUI. Поведение меняет только COMFY_KEEP_EXTRA=1.
_prepare_extra_model_paths() {
    local src="${AUDIO_WORKER_DIR}/extra_model_paths.yaml"
    local dst="${COMFYUI_DIR}/extra_model_paths.yaml"

    if [[ ! -f "${src}" ]]; then
        die "нет ${src}: образ собран некорректно (ожидался COPY docker/extra_model_paths.yaml)"
    fi
    if [[ -f "${dst}" && "${COMFY_KEEP_EXTRA}" == "1" ]]; then
        log "COMFY_KEEP_EXTRA=1: оставляю extra_model_paths.yaml базового образа (${dst})"
        return 0
    fi
    mkdir -p "${COMFYUI_DIR}"
    if [[ "${VOLUME_ROOT}" == "/runpod-volume" ]]; then
        cp -f "${src}" "${dst}"
    else
        # Нестандартный корень тома (локальная отладка): подставляем его путь.
        sed "s#/runpod-volume#${VOLUME_ROOT}#g" "${src}" >"${dst}"
    fi
    log "extra_model_paths.yaml: модели с тома ${VOLUME_ROOT}/models подключены"
}

# --- 3. Старт ComfyUI в фоне -------------------------------------------------

_detect_comfy_python() {
    local candidate
    for candidate in \
        "${COMFYUI_PYTHON}" \
        "${COMFYUI_DIR}/venv/bin/python" \
        "${COMFYUI_DIR}/.venv/bin/python" \
        "${COMFYUI_DIR}/../.venv-cu130/bin/python" \
        "${COMFYUI_DIR}/../.venv/bin/python" \
        "/opt/venv/bin/python"; do
        if [[ -n "${candidate}" && -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi
    return 1
}

_start_comfyui() {
    local py

    if [[ -n "${COMFY_START_CMD}" ]]; then
        log "ComfyUI: старт по COMFY_START_CMD (${COMFY_START_CMD})"
        if [[ -d "${COMFYUI_DIR}" ]]; then
            (cd "${COMFYUI_DIR}" && exec bash -c "${COMFY_START_CMD}") &
        else
            bash -c "${COMFY_START_CMD}" &
        fi
        COMFY_PID=$!
        return 0
    fi

    if [[ ! -d "${COMFYUI_DIR}" ]]; then
        die "нет каталога COMFYUI_DIR=${COMFYUI_DIR}: базовый образ не содержит ComfyUI (нужен образ runpod/comfyui)"
    fi
    if [[ ! -f "${COMFYUI_DIR}/main.py" ]]; then
        die "в ${COMFYUI_DIR} нет main.py: базовый образ не содержит ComfyUI. Stub-сборка на python:3.12-slim не предназначена для запуска"
    fi

    py="$(_detect_comfy_python)" || die "не нашёл Python для ComfyUI (${COMFYUI_DIR}/venv/bin/python, /opt/venv/bin/python, python3)"
    log "ComfyUI: ${py} main.py --listen ${COMFY_HOST} --port ${COMFY_PORT} (cwd=${COMFYUI_DIR})"
    # Логи ComfyUI наследуют stdout/stderr контейнера (PYTHONUNBUFFERED=1).
    (cd "${COMFYUI_DIR}" && exec "${py}" main.py --listen "${COMFY_HOST}" --port "${COMFY_PORT}") &
    COMFY_PID=$!
}

# --- 4. Ожидание /system_stats -----------------------------------------------

_wait_for_comfyui() {
    if [[ ! -x "${WAIT_COMFY_SH}" ]]; then
        die "нет исполняемого ${WAIT_COMFY_SH}: образ собран некорректно"
    fi
    log "ожидаю готовности ComfyUI: ${COMFY_URL}/system_stats (таймаут ${COMFY_BOOT_TIMEOUT_SECONDS}s)"
    if ! "${WAIT_COMFY_SH}" \
        --url "${COMFY_URL}" \
        --timeout "${COMFY_BOOT_TIMEOUT_SECONDS}" \
        --pid "${COMFY_PID}"; then
        die "ComfyUI не ответил на ${COMFY_URL}/system_stats за ${COMFY_BOOT_TIMEOUT_SECONDS}s (см. лог ComfyUI выше)"
    fi
}

# --- 5. Автоскачивание моделей на том ----------------------------------------

# VOLUME_AUTOPOPULATE=true — докачиваем отсутствующие файлы из
# models-manifest.txt на ${VOLUME_ROOT}/models. Ошибка скачивания не валит
# старт воркера: health остаётся доступным, а job'ы на отсутствующих моделях
# падают с видимой ошибкой (лучше, чем «тихо» работать на других весах).
_populate_volume() {
    if [[ "${VOLUME_AUTOPOPULATE}" != "true" ]]; then
        log "VOLUME_AUTOPOPULATE=${VOLUME_AUTOPOPULATE}: автоскачивание моделей выключено"
        return 0
    fi
    if [[ ! -d "${VOLUME_ROOT}" ]]; then
        warn "том ${VOLUME_ROOT} не смонтирован — автоскачивание моделей пропущено (attach network volume)"
        return 0
    fi
    if [[ ! -x "${POPULATE_SH}" ]]; then
        warn "нет исполняемого ${POPULATE_SH}: автоскачивание моделей пропущено"
        return 0
    fi
    log "автоскачивание моделей: ${POPULATE_SH} (манифест ${MANIFEST})"
    if ! MODELS_ROOT="${MODELS_ROOT}" MODELS_MANIFEST="${MANIFEST}" HF_TOKEN="${HF_TOKEN}" "${POPULATE_SH}"; then
        warn "автоскачивание моделей завершилось с ошибкой: job'ы на отсутствующих моделях упадут (health доступен)"
    fi
}

# --- 6. Запуск handler'а -----------------------------------------------------

_resolve_handler_python() {
    if [[ -n "${AUDIO_WORKER_PYTHON}" ]]; then
        if ! command -v "${AUDIO_WORKER_PYTHON}" >/dev/null 2>&1; then
            return 1
        fi
        printf '%s\n' "${AUDIO_WORKER_PYTHON}"
        return 0
    fi
    # Пакеты воркера (runpod/httpx/boto3) стоят в системном python3 образа;
    # venv ComfyUI создан с --system-site-packages, поэтому тоже их видит.
    # Предпочитаем system python3: handler не зависит от venv ComfyUI.
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi
    return 1
}

_start_handler() {
    local py
    py="$(_resolve_handler_python)" || die "не нашёл интерпретатор для handler'а; задайте AUDIO_WORKER_PYTHON"

    # Пакет должен импортироваться до старта handler'а: иначе нет смысла
    # держать GPU. PYTHONPATH ставится в образе; здесь — страховка для
    # случая запуска entrypoint вне образа.
    export PYTHONPATH="${AUDIO_WORKER_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
    if ! "${py}" -c "import audio_worker" 2>/dev/null; then
        die "пакет audio_worker не импортируется интерпретатором ${py}: проверьте слой COPY src/ и PYTHONPATH"
    fi

    log "handler: ${py} ${AUDIO_WORKER_DIR}/handler.py (audio-serverless/v1, ${AUDIO_WORKER_VERSION})"
    "${py}" "${AUDIO_WORKER_DIR}/handler.py" &
    HANDLER_PID=$!
    wait "${HANDLER_PID}"
}

# --- Точка входа -------------------------------------------------------------

main() {
    log "audio-serverless worker ${AUDIO_WORKER_VERSION} (ComfyUI + Stable Audio 3 / ACE-Step 1.5)"
    log "COMFYUI_DIR=${COMFYUI_DIR} COMFYUI_PYTHON=${COMFYUI_PYTHON:-<авто>} COMFY_URL=${COMFY_URL} VOLUME_ROOT=${VOLUME_ROOT} LINK_VOLUME_NODES=${LINK_VOLUME_NODES} VOLUME_AUTOPOPULATE=${VOLUME_AUTOPOPULATE}"

    # Ранняя диагностика: на stub-базе (python:3.12-slim) ComfyUI нет — явная
    # ошибка вместо падения где-то в середине boot. COMFY_START_CMD — исключение:
    # он может сам поднять ComfyUI из нестандартного места.
    if [[ -z "${COMFY_START_CMD}" && ! -f "${COMFYUI_DIR}/main.py" ]]; then
        die "в ${COMFYUI_DIR} нет main.py: базовый образ не содержит ComfyUI. Stub-сборка не предназначена для запуска"
    fi

    _prepare_volume_nodes
    _prepare_extra_model_paths
    _start_comfyui
    _wait_for_comfyui
    _populate_volume

    export COMFY_URL
    _start_handler
}

main "$@"
