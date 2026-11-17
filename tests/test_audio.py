"""Факты аудиофайла из заголовка: WAV/FLAC, пустой и битый файл — ошибки."""

from __future__ import annotations

import pytest
from helpers import flac_bytes, wav_bytes

from audio_worker.audio import read_audio_info
from audio_worker.errors import WorkerError


def test_wav_facts() -> None:
    info = read_audio_info(wav_bytes(2.5, sample_rate=48000, channels=2))
    assert info.codec == "wav"
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert info.duration_seconds == pytest.approx(2.5, abs=1e-3)


def test_flac_facts() -> None:
    info = read_audio_info(flac_bytes(1.25, sample_rate=32000, channels=1))
    assert info.codec == "flac"
    assert info.sample_rate == 32000
    assert info.channels == 1
    assert info.duration_seconds == pytest.approx(1.25, abs=1e-3)


def test_empty_audio_is_output_empty() -> None:
    with pytest.raises(WorkerError) as error:
        read_audio_info(b"")
    assert error.value.code == "output_empty"


def test_broken_audio_is_output_invalid() -> None:
    with pytest.raises(WorkerError) as error:
        read_audio_info(b"not-an-audio-file")
    assert error.value.code == "output_invalid"


def test_zero_frame_wav_is_output_empty() -> None:
    # Валидный WAV-заголовок без кадров: файл не пустой, но звука нет.
    with pytest.raises(WorkerError) as error:
        read_audio_info(wav_bytes(0.0))
    assert error.value.code == "output_empty"
