"""Health: статусы ok/starting/degraded по тому и /system_stats, без графа."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from audio_worker.config import Config
from audio_worker.handler import Dependencies, execute
from audio_worker.health import health_payload


class ComfyStub:
    def __init__(self, *, stats: Any = None, last_error: str | None = None, raises: bool = False) -> None:
        self._stats = stats if stats is not None else {"system": {}}
        self.last_error = last_error
        self._raises = raises
        self.calls = 0

    async def system_stats(self, timeout: float | None = None) -> Any:
        self.calls += 1
        if self._raises:
            raise RuntimeError("comfy exploded")
        return self._stats


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _config(tmp_path: Path, *, volume: bool = True) -> Config:
    volume_root = tmp_path / "volume"
    if volume:
        volume_root.mkdir(parents=True, exist_ok=True)
    return Config.from_env(
        {
            "VOLUME_ROOT": str(volume_root),
            "HEALTH_TIMEOUT_SECONDS": "1",
        }
    )


def test_ready_and_volume_mounted(tmp_path: Path) -> None:
    result = _run(health_payload(_config(tmp_path), ComfyStub()))
    assert result == {"status": "ok", "comfyui": "ready", "volume_mounted": True}


def test_booting_with_volume_is_starting(tmp_path: Path) -> None:
    result = _run(health_payload(_config(tmp_path), ComfyStub(stats={}, last_error="connect")))
    assert result["comfyui"] == "booting"
    assert result["status"] == "starting"


def test_unavailable_without_volume_is_degraded(tmp_path: Path) -> None:
    result = _run(
        health_payload(_config(tmp_path, volume=False), ComfyStub(stats={}, last_error="error"))
    )
    assert result == {"status": "degraded", "comfyui": "unavailable", "volume_mounted": False}


def test_system_stats_exception_does_not_break_health(tmp_path: Path) -> None:
    result = _run(health_payload(_config(tmp_path), ComfyStub(raises=True)))
    assert result["status"] == "degraded"
    assert result["comfyui"] == "unavailable"


def test_no_executor_is_degraded(tmp_path: Path) -> None:
    result = _run(health_payload(_config(tmp_path), None))
    assert result["comfyui"] == "unavailable"
    assert result["status"] == "degraded"


def test_health_action_does_not_touch_graph_or_model(tmp_path: Path, setup) -> None:
    _, manifest, _ = setup
    config = _config(tmp_path)

    class Executor(ComfyStub):
        async def execute(self, graph: Any, timeout_seconds: float) -> Any:  # pragma: no cover
            raise AssertionError("health не должен запускать граф")

    deps = Dependencies(config=config, executor=Executor(), manifest=manifest)
    result = _run(execute({"id": "h1", "input": {"action": "health"}}, deps))
    assert result == {"status": "ok", "comfyui": "ready", "volume_mounted": True}
