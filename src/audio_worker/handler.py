"""RunPod handler контракта `audio-serverless/v1`.

Поток: валидация job/input по схеме → лимит длительности по ``kind`` → факты
исполненного графа (файл модели, seed) → проверка pinned-модели на томе
(``model_file_missing`` / ``provision_hash_mismatch``) → исполнение готового
графа в ComfyUI → факты произведённого файла → телеметрия из исполнения →
доставка base64/URL → финальный гейт output-схемой.

Воркер **не выбирает модель** (`parameter.model_slug` — tracing): исполняется
тот граф, который прислал сервис; телеметрия берётся из графа, файла и манифеста.
Внешние зависимости инъектируются (``build_handler``/``Dependencies``): тесты
идут без сети, ComfyUI, GPU и runpod.

Ссылки: `contract/worker-contract.md` §1/§2/§5.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio import read_audio_info
from .comfy_client import AudioExecution, AudioExecutor, ComfyAudioExecutor
from .config import Config
from .contract import validate_input, validate_output
from .errors import WorkerError
from .health import health_payload
from .manifest import ModelFile, ModelManifest, sha256_file
from .storage import AudioStorage, S3Storage, deliver_audio
from .telemetry import build_telemetry, graph_model_files, graph_seed

_LOG = logging.getLogger(__name__)

# Лимиты длительности (contract §1.1, plan §4.1): по запросу/снимку параметров.
DURATION_LIMITS: dict[str, float] = {"sfx": 30.0, "layer": 30.0, "music": 90.0}

ModelHasher = Callable[[Path], str]


@dataclass
class Dependencies:
    """Всё внешнее, что нужно handler'у (инъекция для тестов)."""

    config: Config
    executor: AudioExecutor
    manifest: ModelManifest
    storage: AudioStorage | None = None
    model_hasher: ModelHasher = sha256_file


def _job_id(job: Any) -> str:
    if isinstance(job, Mapping) and job.get("id") is not None:
        return str(job.get("id"))
    return "<без id>"


def _validate_job(job: Any) -> Mapping[str, Any]:
    if not isinstance(job, Mapping):
        raise WorkerError("input_invalid", "job должен быть объектом RunPod")
    payload = job.get("input")
    if not isinstance(payload, Mapping):
        raise WorkerError("input_invalid", "job.input отсутствует или не является объектом")
    return payload


def check_duration(kind: str, duration_seconds: float) -> None:
    """Второй гейт длительности до GPU: сервис проверяет тот же лимит (§7.1)."""

    limit = DURATION_LIMITS.get(kind)
    if limit is None:
        raise WorkerError("input_invalid", f"parameter.kind={kind!r}: неизвестный kind")
    if float(duration_seconds) > limit:
        raise WorkerError(
            "duration_limit_exceeded",
            f"kind={kind}: {duration_seconds} с > лимита {limit:g} с",
        )


def _resolve_model(
    graph: Mapping[str, Any], deps: Dependencies
) -> tuple[ModelFile, Path]:
    """Основная модель — только из исполненного графа, не из parameter."""

    filename = graph_model_files(graph)[0]
    entry = deps.manifest.by_filename(filename)
    if entry is None:
        raise WorkerError(
            "telemetry_missing",
            f"модель {filename!r} из графа не найдена в models-manifest.txt: "
            "engine_version/model_hash/license не измерить",
        )
    return entry, deps.config.models_root / entry.relative_path


def _measure_model_hash(entry: ModelFile, path: Path, deps: Dependencies) -> str:
    """sha256 исполненного файла, сверенный с pinned-манифестом (§7.3)."""

    if not path.is_file():
        raise WorkerError(
            "model_file_missing",
            f"{path} нет на томе: прогнать populate-volume.sh для {entry.filename}",
        )
    actual = deps.model_hasher(path)
    if actual != entry.sha256:
        raise WorkerError(
            "provision_hash_mismatch",
            f"{entry.filename}: sha256 {actual} != pinned {entry.sha256}",
        )
    return actual


async def execute(job: Any, deps: Dependencies) -> dict[str, Any]:
    """Выполнить один job; ошибки — ``WorkerError`` наружу (job FAILED)."""

    payload = _validate_job(job)
    # Схема описывает тело job'а: {"input": ..., "policy": ...}; RunPod добавляет
    # к нему ``id`` (top-level additionalProperties: true).
    validate_input(job, deps.config.contract_dir)

    # Health (contract §3): без графа, модели и телеметрии — только том и ComfyUI.
    if payload.get("action") == "health":
        return await health_payload(deps.config, deps.executor)

    parameter = payload.get("parameter")
    if not isinstance(parameter, Mapping):  # защита от рассогласования схемы и кода
        raise WorkerError("input_invalid", "input.parameter отсутствует")
    graph = payload.get("prompt")
    if not isinstance(graph, Mapping):
        raise WorkerError("input_invalid", "input.prompt отсутствует")

    kind = str(parameter.get("kind"))
    duration_seconds = float(parameter.get("duration_seconds", 0))
    check_duration(kind, duration_seconds)

    entry, model_path = _resolve_model(graph, deps)
    model_sha256 = _measure_model_hash(entry, model_path, deps)
    seed = graph_seed(graph)
    _LOG.info(
        "job %s: kind=%s, модель из графа=%s, seed=%s",
        _job_id(job),
        kind,
        entry.filename,
        seed,
    )

    execution: AudioExecution = await deps.executor.execute(
        graph, deps.config.job_timeout_seconds
    )
    audio = execution.audio
    if not audio:
        raise WorkerError("output_empty", "ComfyUI вернул 0 байт аудио — это не успех")
    info = read_audio_info(audio)

    telemetry = build_telemetry(
        entry,
        seed=seed,
        info=info,
        gpu_seconds=execution.elapsed_seconds,
    )
    # model_hash — измеренный файл, а не pinned-значение: build_telemetry берёт
    # entry (pinned), поэтому подменяем на факт, уже сверенный выше.
    telemetry["model_hash"] = f"sha256:{model_sha256}"

    delivery = await deliver_audio(audio, info, deps.config, deps.storage)
    result: dict[str, Any] = {**delivery, "telemetry": telemetry}
    validate_output(result, deps.config.contract_dir)
    _LOG.info(
        "job %s: готово, %.2f с, %d байт, seed=%s, доставка=%s",
        _job_id(job),
        info.duration_seconds,
        len(audio),
        seed,
        "audio_url" if "audio_url" in delivery else "inline base64",
    )
    return result


def build_handler(
    config: Config,
    executor: AudioExecutor,
    manifest: ModelManifest,
    storage: AudioStorage | None = None,
    *,
    model_hasher: ModelHasher = sha256_file,
) -> Callable[[Any], Awaitable[dict[str, Any]]]:
    """Собрать handler с зафиксированными зависимостями (для тестов и main)."""

    deps = Dependencies(
        config=config,
        executor=executor,
        manifest=manifest,
        storage=storage,
        model_hasher=model_hasher,
    )

    async def handler(job: Any) -> dict[str, Any]:
        return await execute(job, deps)

    return handler


# Ленивый дефолт для RunPod: config/manifest/executor создаются при первом job,
# чтобы импорт модуля (и тесты) не требовали ComfyUI, том и S3.
_default_deps: Dependencies | None = None


def default_dependencies() -> Dependencies:
    """Зависимости по умолчанию из env (локальный ComfyUI, том, манифест)."""

    global _default_deps
    if _default_deps is None:
        config = Config.from_env()
        manifest = ModelManifest.load(config.manifest_path)
        storage = S3Storage(config) if config.storage_configured else None
        _default_deps = Dependencies(
            config=config,
            executor=ComfyAudioExecutor.from_config(config),
            manifest=manifest,
            storage=storage,
        )
    return _default_deps


async def handler(job: Any) -> dict[str, Any]:
    """Точка входа RunPod (`handler.py` в корне); тесты подменяют зависимости."""

    return await execute(job, default_dependencies())


__all__ = [
    "DURATION_LIMITS",
    "Dependencies",
    "build_handler",
    "check_duration",
    "default_dependencies",
    "execute",
    "handler",
]
