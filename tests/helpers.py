"""Test helpers: audio bytes, ComfyUI audio graphs, pinned manifest, fakes.

Tests need no GPU, no network, no ComfyUI and no runpod SDK: the executor and
storage are injected fakes, exactly like the live worker injects the HTTP
ComfyUI client and S3 storage.
"""

from __future__ import annotations

import hashlib
import io
import json
import wave
from pathlib import Path
from typing import Any

from audio_worker.comfy_client import AudioExecution

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contract"
PIN = "a" * 40

# filename -> (subdir, version, license) for the example graphs.
MANIFEST_MODELS: dict[str, tuple[str, str, str]] = {
    "stable_audio_3_small_sfx.safetensors": (
        "checkpoints",
        "stable-audio-3-small-sfx@2026-05",
        "Stability-AI-Community",
    ),
    "t5gemma_b_b_ul2.safetensors": (
        "text_encoders",
        "stable-audio-3-small-sfx@2026-05",
        "Stability-AI-Community",
    ),
    "ace_step_1.5_turbo_aio.safetensors": (
        "checkpoints",
        "ace-step-1.5@2026-04",
        "MIT",
    ),
}


def contract_dir() -> Path:
    return CONTRACT


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def wav_bytes(
    duration_seconds: float = 2.0,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
) -> bytes:
    """Valid silent PCM s16le WAV with the requested facts."""

    frames = max(0, int(round(float(duration_seconds) * sample_rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * channels * frames)
    return buffer.getvalue()


def flac_bytes(
    duration_seconds: float = 2.0,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
) -> bytes:
    """Minimal FLAC container with a STREAMINFO block (header facts only)."""

    total_samples = max(1, int(round(float(duration_seconds) * sample_rate)))
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((16 - 1) << 36) | total_samples
    streaminfo = (
        (4096).to_bytes(2, "big")
        + (4096).to_bytes(2, "big")
        + (0).to_bytes(3, "big")
        + (0).to_bytes(3, "big")
        + packed.to_bytes(8, "big")
        + b"\x00" * 16
    )
    header = bytes([0x80 | 0x00]) + len(streaminfo).to_bytes(3, "big")
    return b"fLaC" + header + streaminfo


def model_content(filename: str) -> bytes:
    return f"weights:{filename}".encode()


def model_sha(filename: str) -> str:
    return hashlib.sha256(model_content(filename)).hexdigest()


def manifest_line(filename: str) -> str:
    subdir, version, license_id = MANIFEST_MODELS[filename]
    url = f"https://huggingface.co/example/repo/resolve/{PIN}/{subdir}/{filename}"
    return "\t".join([subdir, filename, url, model_sha(filename), version, license_id])


def write_models(models_root: Path) -> Path:
    """Write fake weight files and a matching pinned manifest; return manifest path."""

    lines = [
        "# test manifest",
        *[manifest_line(filename) for filename in MANIFEST_MODELS],
    ]
    for filename, (subdir, _version, _license) in MANIFEST_MODELS.items():
        target = models_root / subdir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(model_content(filename))
    manifest_path = models_root.parent / "models-manifest.txt"
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path


def sfx_graph(
    *,
    seed: int = 20261116,
    ckpt_name: str = "stable_audio_3_small_sfx.safetensors",
) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": "t5gemma_b_b_ul2.safetensors", "type": "stable_audio"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0], "text": "heavy metal door slam"},
        },
        "4": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["3", 0]}},
        "5": {"class_type": "EmptyLatentAudio", "inputs": {"seconds": 2.0, "batch_size": 1}},
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["5", 0],
                "seed": seed,
                "steps": 8,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {
            "class_type": "SaveAudioAdvanced",
            "inputs": {"audio": ["7", 0], "filename_prefix": "audio/sfx", "format": "wav"},
        },
    }


def music_graph(*, seed: int = 20261117) -> dict[str, Any]:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "ace_step_1.5_turbo_aio.safetensors"},
        },
        "2": {
            "class_type": "TextEncodeAceStepAudio1.5",
            "inputs": {"clip": ["1", 1], "tags": "noir jazz", "lyrics": ""},
        },
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {"class_type": "EmptyAceStep1.5LatentAudio", "inputs": {"seconds": 60, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": 8,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveAudioAdvanced",
            "inputs": {"audio": ["6", 0], "filename_prefix": "audio/music", "format": "wav"},
        },
    }


class FakeExecutor:
    """Подменяет HTTP-исполнитель ComfyUI: без сети и GPU."""

    def __init__(self, audio: bytes | None = None, *, elapsed_seconds: float = 1.5) -> None:
        self.audio = wav_bytes() if audio is None else audio
        self.elapsed_seconds = elapsed_seconds
        self.graphs: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    async def execute(self, graph: dict[str, Any], timeout_seconds: float) -> AudioExecution:
        self.graphs.append(graph)
        self.timeouts.append(timeout_seconds)
        return AudioExecution(audio=self.audio, elapsed_seconds=self.elapsed_seconds)


class FakeStorage:
    """Подменяет S3: запоминает загруженные байты и отдаёт presigned URL."""

    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str]] = []

    async def upload(self, data: bytes, content_type: str) -> str:
        self.uploads.append((data, content_type))
        return f"https://storage.example/audio/take-{len(self.uploads)}.wav?signature=test"
