"""Исполнитель готового ComfyUI API-графа для audio-стенда.

HTTP-обвязка снята с image-serverless (submit → history → /view), но **живой
аудио-стек ещё не проверен на стенде**: см. ``TODO(live)`` ниже. Модули
``httpx`` не импортируются на уровне модуля, поэтому handler и тесты работают
без сети, ComfyUI и GPU (исполнитель инъектируется: ``AudioExecutor``).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .config import Config
from .errors import WorkerError

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioExecution:
    """Факты одного исполнения графа: байты аудио и секунды исполнения."""

    audio: bytes
    elapsed_seconds: float


class AudioExecutor(Protocol):
    """Контракт исполнителя: граф → аудио + измеренные секунды."""

    async def execute(self, graph: Mapping[str, Any], timeout_seconds: float) -> AudioExecution:
        ...


class ComfyAudioExecutor:
    """Тонкий HTTP-клиент локального ComfyUI (loopback only).

    TODO(live): проверить на стенде ``audio``:
      1. аудио-ноды ComfyUI в базовом образе (Stable Audio 3 / ACE-Step 1.5)
         и ключ ``audio`` в ``/history.outputs``;
      2. формат файла из ``SaveAudioAdvanced``: контракт требует wav — при
         ``flac`` телеметрия читается (см. ``audio.py``), mp3/opus запрещены;
      3. ``POST /interrupt`` на отмене job;
      4. cold start ComfyUI и ``policy.executionTimeout`` ≥ ``JOB_TIMEOUT_SECONDS``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        poll_initial_seconds: float = 0.5,
        poll_max_seconds: float = 3.0,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._poll_initial_seconds = poll_initial_seconds
        self._poll_max_seconds = poll_max_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._client: Any = None
        self._client_id = uuid.uuid4().hex
        # Причина последнего отказа system_stats — для health (booting/unavailable).
        self.last_error: str | None = None

    @classmethod
    def from_config(cls, config: Config) -> "ComfyAudioExecutor":
        return cls(
            config.comfy_url,
            poll_initial_seconds=config.comfy_poll_initial_seconds,
            poll_max_seconds=config.comfy_poll_max_seconds,
            request_timeout_seconds=config.comfy_request_timeout_seconds,
        )

    async def system_stats(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Одиночный probe ``GET /system_stats`` для health (contract §3).

        Сбои не поднимаются исключением: ``last_error`` получает класс отказа
        (``timeout``/``connect`` — ComfyUI ещё стартует, иначе текст ошибки),
        а health решает, это ``booting`` или ``unavailable``. Успех — ``None``.
        """

        limit = self._request_timeout_seconds if timeout is None else timeout
        try:
            response = await self._http().get("/system_stats", timeout=limit)
        except Exception as exc:  # noqa: BLE001 - health не должен падать
            self.last_error = "timeout" if "Timeout" in type(exc).__name__ else "connect"
            return None
        if response.status_code >= 400:
            self.last_error = f"HTTP {response.status_code}"
            return None
        try:
            payload = response.json()
        except ValueError:
            self.last_error = "ответ /system_stats — не JSON"
            return None
        if not isinstance(payload, dict):
            self.last_error = "ответ /system_stats — не объект"
            return None
        self.last_error = None
        return payload

    async def execute(self, graph: Mapping[str, Any], timeout_seconds: float) -> AudioExecution:
        started = time.monotonic()
        prompt_id = await self._submit(graph)
        try:
            history = await self._wait(prompt_id, timeout_seconds)
        except asyncio.CancelledError:
            _LOG.warning("job отменён: прерываю ComfyUI prompt_id=%s", prompt_id)
            await self._interrupt()
            raise
        audio = await self._fetch_output(history)
        return AudioExecution(audio=audio, elapsed_seconds=time.monotonic() - started)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> Any:
        if self._client is None:
            try:
                import httpx  # локальный импорт: import handler не требует httpx
            except ImportError as exc:  # pragma: no cover - образ ставит httpx
                raise WorkerError("config_invalid", "httpx не установлен в образе воркера") from exc
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._request_timeout_seconds,
            )
        return self._client

    async def _submit(self, graph: Mapping[str, Any]) -> str:
        response = await self._http().post(
            "/prompt",
            json={"prompt": dict(graph), "client_id": self._client_id},
        )
        if response.status_code != 200:
            raise WorkerError(
                "comfy_submit_failed",
                f"POST /prompt -> {response.status_code}: {response.text[:400]}",
            )
        payload = response.json()
        prompt_id = payload.get("prompt_id") or payload.get("id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise WorkerError("comfy_submit_failed", f"в ответе /prompt нет prompt_id: {payload!r}")
        return prompt_id

    async def _wait(self, prompt_id: str, timeout_seconds: float) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        delay = self._poll_initial_seconds
        while time.monotonic() < deadline:
            response = await self._http().get(f"/history/{prompt_id}")
            if response.status_code == 200:
                payload = response.json()
                entry = payload.get(prompt_id)
                if isinstance(entry, Mapping):
                    status = entry.get("status") or {}
                    status_str = status.get("status_str") if isinstance(status, Mapping) else None
                    if status_str == "error":
                        raise WorkerError(
                            "comfy_execution_failed",
                            f"ComfyUI history error: {status!r}",
                        )
                    if status_str == "success" or entry.get("outputs"):
                        return entry
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, self._poll_max_seconds)
        raise WorkerError("comfy_timeout", f"граф не завершился за {timeout_seconds} с")

    async def _fetch_output(self, history: Mapping[str, Any]) -> bytes:
        outputs = history.get("outputs")
        if not isinstance(outputs, Mapping):
            raise WorkerError("comfy_output_missing", "в /history нет outputs")
        reference: Mapping[str, Any] | None = None
        for node_output in outputs.values():
            if not isinstance(node_output, Mapping):
                continue
            audio = node_output.get("audio")
            if isinstance(audio, list) and audio and isinstance(audio[0], Mapping):
                reference = audio[0]
                break
        if reference is None:
            raise WorkerError("comfy_output_missing", "в outputs нет audio-файла (save-audio нода)")
        # Некоторые save-ноды возвращают bytes в base64 вместо /view-ссылки.
        inline = reference.get("data")
        if isinstance(inline, str) and inline:
            try:
                return base64.b64decode(inline, validate=True)
            except ValueError as exc:
                raise WorkerError("comfy_output_missing", f"битый base64 в outputs.audio: {exc}") from exc
        filename = reference.get("filename")
        if not isinstance(filename, str) or not filename:
            raise WorkerError("comfy_output_missing", f"в outputs.audio нет filename: {reference!r}")
        response = await self._http().get(
            "/view",
            params={
                "filename": filename,
                "subfolder": str(reference.get("subfolder") or ""),
                "type": str(reference.get("type") or "output"),
            },
        )
        if response.status_code != 200:
            raise WorkerError(
                "comfy_output_missing",
                f"GET /view -> {response.status_code}: {response.text[:200]}",
            )
        return response.content

    async def _interrupt(self) -> None:
        try:
            await self._http().post("/interrupt")
        except Exception:  # noqa: BLE001 - отмена не должна маскироваться сетевой ошибкой
            _LOG.exception("не удалось прервать ComfyUI")


__all__ = ["AudioExecution", "AudioExecutor", "ComfyAudioExecutor"]
