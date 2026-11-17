"""Телеметрия из фактов исполнения (contract/worker-contract.md §2.2).

Значения берутся только из исполненного графа, произведённого файла и
pinned-манифеста. ``parameter`` — снимок сервиса — не подставляется никогда.
"""

from __future__ import annotations

from typing import Any, Mapping

from .audio import AudioInfo
from .errors import WorkerError
from .manifest import ModelFile

# class_type -> имя input'а с файлом основной модели (не текстовый энкодер/VAE).
MODEL_LOADERS: dict[str, str] = {
    "CheckpointLoaderSimple": "ckpt_name",
    "CheckpointLoader": "ckpt_name",
    "UNETLoader": "unet_name",
    "UnetLoaderGGUF": "unet_name",
    "UnetLoaderGGUFAdvanced": "unet_name",
}

_SEED_SAMPLERS = ("KSampler", "KSamplerAdvanced")
_CUSTOM_SAMPLERS = ("SamplerCustom", "SamplerCustomAdvanced")


def _nodes(graph: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nodes = []
    for node in graph.values():
        if isinstance(node, Mapping) and isinstance(node.get("class_type"), str):
            nodes.append(node)
    return nodes


def _linked_value(graph: Mapping[str, Any], value: Any) -> Any:
    """Развернуть link ``[node_id, slot]`` в значение указанной ноды."""

    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return value
    node_id, _slot = value
    node = graph.get(str(node_id))
    if not isinstance(node, Mapping):
        return None
    return node


def _seed_value(graph: Mapping[str, Any], value: Any, label: str) -> int:
    if isinstance(value, (list, tuple)):
        linked = _linked_value(graph, value)
        if isinstance(linked, Mapping):
            class_type = linked.get("class_type")
            inputs = linked.get("inputs")
            if class_type == "RandomNoise" and isinstance(inputs, Mapping):
                value = inputs.get("noise_seed")
    if isinstance(value, bool):
        raise WorkerError("telemetry_missing", f"{label}: seed не число (bool)")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise WorkerError("telemetry_missing", f"{label}: seed не измерить (значение {value!r})")


def graph_model_files(graph: Mapping[str, Any]) -> tuple[str, ...]:
    """Имена файлов основной модели из исполненного графа (ровно одно)."""

    files: list[str] = []
    for node in _nodes(graph):
        key = MODEL_LOADERS.get(str(node.get("class_type")))
        if key is None:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        value = inputs.get(key)
        if isinstance(value, str) and value:
            files.append(value)
    unique = tuple(dict.fromkeys(files))
    if not unique:
        raise WorkerError(
            "telemetry_missing",
            "в графе нет loader-ноды модели (ckpt_name/unet_name): "
            "engine_version/model_hash/license не измерить",
        )
    if len(unique) > 1:
        raise WorkerError(
            "telemetry_ambiguous",
            f"в графе несколько моделей: {sorted(unique)}",
        )
    return unique


def graph_seed(graph: Mapping[str, Any]) -> int:
    """Seed из исполненного графа; противоречие/отсутствие — ошибка."""

    seeds: list[int] = []
    for node in _nodes(graph):
        class_type = str(node.get("class_type"))
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        if class_type in _SEED_SAMPLERS:
            seeds.append(_seed_value(graph, inputs.get("seed"), f"{class_type}.seed"))
        elif class_type in _CUSTOM_SAMPLERS:
            value = inputs.get("seed")
            if value is None:
                value = inputs.get("noise_seed")
            seeds.append(_seed_value(graph, value, f"{class_type}.seed"))
    unique = sorted(set(seeds))
    if not unique:
        raise WorkerError(
            "telemetry_missing",
            "в графе нет seed сэмплера (KSampler.seed / RandomNoise.noise_seed)",
        )
    if len(unique) > 1:
        raise WorkerError(
            "telemetry_ambiguous",
            f"в графе разные seed сэмплеров: {unique}",
        )
    return unique[0]


def build_telemetry(
    entry: ModelFile,
    *,
    seed: int,
    info: AudioInfo,
    gpu_seconds: float,
) -> dict[str, Any]:
    """Собрать телеметрию контракта; все значения — факты исполнения."""

    return {
        "engine_version": entry.version,
        "model_hash": f"sha256:{entry.sha256}",
        "seed": int(seed),
        "duration_seconds": round(float(info.duration_seconds), 3),
        "sample_rate": int(info.sample_rate),
        "channels": int(info.channels),
        "gpu_seconds": max(round(float(gpu_seconds), 3), 0.001),
        "license": entry.license,
    }


__all__ = ["build_telemetry", "graph_model_files", "graph_seed"]
