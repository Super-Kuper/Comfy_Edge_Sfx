"""audio_worker — RunPod serverless воркер SFX/музыки (contract audio-serverless/v1).

Пакет не импортирует runpod/httpx/torch на уровне модулей: handler и тесты
работают без GPU, сети и ComfyUI; тяжёлое — локальные импорты и инъекции.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
