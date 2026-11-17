"""Handler: контракт, факты исполнения, пустой результат, «модель не выбирается»."""

from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import sys

import pytest
from helpers import (
    MANIFEST_MODELS,
    FakeExecutor,
    FakeStorage,
    contract_dir,
    flac_bytes,
    load_json,
    model_sha,
    music_graph,
    sfx_graph,
    wav_bytes,
)
from jsonschema import Draft202012Validator

from audio_worker.errors import WorkerError
from audio_worker.handler import DURATION_LIMITS, build_handler

# --- Вспомогательное ---------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _job(
    graph: dict | None = None,
    *,
    kind: str = "sfx",
    duration: float = 2.0,
    seed: int | None = 7,
    model_slug: str = "stable-audio-3-small-sfx",
    **input_overrides,
) -> dict:
    payload = {
        "prompt": copy.deepcopy(sfx_graph()) if graph is None else graph,
        "parameter": {
            "kind": kind,
            "duration_seconds": duration,
            "seed": seed,
            "prompt_hash": "a" * 64,
            "model_slug": model_slug,
        },
        "assets": {},
    }
    payload.update(input_overrides)
    return {"id": "job-1", "input": payload}


def _handler(setup, executor: FakeExecutor | None = None, storage=None, **kwargs):
    config, manifest, _models_root = setup
    executor = executor or FakeExecutor()
    return build_handler(config, executor, manifest, storage=storage, **kwargs), executor


def _output_validator() -> Draft202012Validator:
    return Draft202012Validator(load_json(contract_dir() / "output.schema.json"))


# --- Happy path --------------------------------------------------------------


def test_success_returns_audio_and_execution_telemetry(setup) -> None:
    handler, executor = _handler(setup, FakeExecutor(wav_bytes(2.0), elapsed_seconds=1.5))
    result = _run(handler(_job()))

    assert set(result) == {"audio_b64", "telemetry"}
    assert base64.b64decode(result["audio_b64"]) == executor.audio
    telemetry = result["telemetry"]
    assert telemetry["model_hash"] == "sha256:" + model_sha("stable_audio_3_small_sfx.safetensors")
    assert telemetry["engine_version"] == MANIFEST_MODELS["stable_audio_3_small_sfx.safetensors"][1]
    assert telemetry["license"] == "Stability-AI-Community"
    # seed — факт исполненного графа, а не снимок parameter.seed=7.
    assert telemetry["seed"] == 20261116
    assert telemetry["duration_seconds"] == pytest.approx(2.0, abs=1e-3)
    assert telemetry["sample_rate"] == 48000
    assert telemetry["channels"] == 2
    assert telemetry["gpu_seconds"] == pytest.approx(1.5)
    _output_validator().validate(result)


def test_flac_audio_is_supported(setup) -> None:
    handler, _ = _handler(setup, FakeExecutor(flac_bytes(1.25, sample_rate=32000, channels=1)))
    result = _run(handler(_job()))
    telemetry = result["telemetry"]
    assert (telemetry["sample_rate"], telemetry["channels"]) == (32000, 1)
    assert telemetry["duration_seconds"] == pytest.approx(1.25, abs=1e-3)


def test_music_graph_uses_its_own_model_facts(setup) -> None:
    storage = FakeStorage()
    handler, executor = _handler(setup, FakeExecutor(wav_bytes(60.0)), storage=storage)
    result = _run(handler(_job(music_graph(), kind="music", duration=60.0)))
    telemetry = result["telemetry"]
    assert executor.graphs[0] == music_graph()
    assert telemetry["engine_version"] == MANIFEST_MODELS["ace_step_1.5_turbo_aio.safetensors"][1]
    assert telemetry["license"] == "MIT"
    assert telemetry["model_hash"] == "sha256:" + model_sha("ace_step_1.5_turbo_aio.safetensors")
    # 60 с 48 кГц стерео не влезает в тестовый inline-лимит (1 MB) — уходит в URL.
    assert set(result) == {"audio_url", "telemetry"}
    assert len(storage.uploads) == 1


def test_graph_is_submitted_unchanged(setup) -> None:
    graph = sfx_graph(seed=123)
    handler, executor = _handler(setup)
    _run(handler(_job(graph)))
    assert executor.graphs == [graph]


def test_telemetry_facts_ignore_parameter_snapshot(setup) -> None:
    """Длительность и seed — факты файла/графа, а не снимок parameter."""

    handler, _ = _handler(setup, FakeExecutor(wav_bytes(3.5), elapsed_seconds=1.0))
    result = _run(handler(_job(duration=2.0, seed=7)))
    telemetry = result["telemetry"]
    assert telemetry["duration_seconds"] == pytest.approx(3.5, abs=1e-3)
    assert telemetry["seed"] == 20261116


# --- Пустой / битый результат ------------------------------------------------


def test_empty_audio_is_an_error(setup) -> None:
    handler, _ = _handler(setup, FakeExecutor(b""))
    with pytest.raises(WorkerError) as error:
        _run(handler(_job()))
    assert error.value.code == "output_empty"


def test_broken_audio_is_an_error(setup) -> None:
    handler, _ = _handler(setup, FakeExecutor(b"not-an-audio"))
    with pytest.raises(WorkerError) as error:
        _run(handler(_job()))
    assert error.value.code == "output_invalid"


# --- «Воркер не выбирает модель» --------------------------------------------


def test_worker_does_not_choose_model_from_snapshot(setup) -> None:
    """parameter.model_slug — tracing: исполнение и телеметрия от графа."""

    handler, executor = _handler(setup)
    graph = sfx_graph()
    first = _run(handler(_job(graph, model_slug="stable-audio-3-small-sfx")))
    second = _run(handler(_job(graph, model_slug="ace-step-1-5")))

    assert executor.graphs == [graph, graph]
    assert first["telemetry"] == second["telemetry"]
    assert first["telemetry"]["engine_version"] == "stable-audio-3-small-sfx@2026-05"


def test_model_choice_cannot_be_injected_into_input(setup) -> None:
    handler, executor = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job(model="ace-step-1-5")))
    assert error.value.code == "input_invalid"
    assert executor.graphs == []


def test_unknown_graph_model_is_telemetry_missing(setup) -> None:
    handler, executor = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job(sfx_graph(ckpt_name="other.safetensors"))))
    assert error.value.code == "telemetry_missing"
    assert executor.graphs == []


def test_two_graph_models_are_telemetry_ambiguous(setup) -> None:
    graph = sfx_graph()
    graph["9"] = {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": "ace_step_1.5_turbo_aio.safetensors", "weight_dtype": "default"},
    }
    handler, executor = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job(graph)))
    assert error.value.code == "telemetry_ambiguous"
    assert executor.graphs == []


def test_graph_without_seed_is_telemetry_missing(setup) -> None:
    graph = sfx_graph()
    graph["6"]["inputs"] = {"model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0]}
    handler, executor = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job(graph)))
    assert error.value.code == "telemetry_missing"
    assert executor.graphs == []


# --- Модель на томе ----------------------------------------------------------


def test_missing_model_file(setup, tmp_path) -> None:
    _config, _manifest, models_root = setup
    (models_root / "checkpoints" / "stable_audio_3_small_sfx.safetensors").unlink()
    handler, executor = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job()))
    assert error.value.code == "model_file_missing"
    assert executor.graphs == []


def test_hash_mismatch_is_provision_hash_mismatch(setup) -> None:
    _config, _manifest, models_root = setup
    (models_root / "checkpoints" / "stable_audio_3_small_sfx.safetensors").write_bytes(b"tampered")
    handler, _ = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job()))
    assert error.value.code == "provision_hash_mismatch"


# --- Лимиты и валидация ------------------------------------------------------


@pytest.mark.parametrize(
    "kind,duration,code",
    [
        ("sfx", DURATION_LIMITS["sfx"] + 0.1, "duration_limit_exceeded"),
        ("layer", DURATION_LIMITS["layer"] + 0.1, "duration_limit_exceeded"),
        ("music", DURATION_LIMITS["music"] + 0.1, "duration_limit_exceeded"),
        ("video", 1.0, "input_invalid"),
    ],
)
def test_duration_limits_fail_before_gpu(setup, kind: str, duration: float, code: str) -> None:
    handler, executor = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job(kind=kind, duration=duration)))
    assert error.value.code == code
    assert executor.graphs == []


def test_limit_boundary_is_allowed(setup) -> None:
    handler, executor = _handler(setup)
    _run(handler(_job(kind="sfx", duration=DURATION_LIMITS["sfx"])))
    assert len(executor.graphs) == 1


@pytest.mark.parametrize(
    "job",
    [
        None,
        {"id": "x"},
        {"id": "x", "input": "not-an-object"},
        {"id": "x", "input": {"prompt": {}}},
    ],
)
def test_invalid_job_is_input_invalid(setup, job: object) -> None:
    handler, _ = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(job))
    assert error.value.code == "input_invalid"


def test_assets_must_be_empty(setup) -> None:
    handler, executor = _handler(setup)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job(assets={"input.wav": "https://files.example/input.wav"})))
    assert error.value.code == "input_invalid"
    assert executor.graphs == []


# --- Доставка: base64 vs URL -------------------------------------------------


def _small_inline_config(config):
    return dataclasses.replace(
        config,
        output_inline_max_mb=0.0001,
        output_inline_budget_mb=0.0001,
    )


def test_large_audio_goes_to_storage_url(setup) -> None:
    config, manifest, _models_root = setup
    storage = FakeStorage()
    executor = FakeExecutor(wav_bytes(2.0))
    handler = build_handler(_small_inline_config(config), executor, manifest, storage=storage)
    result = _run(handler(_job()))

    assert set(result) == {"audio_url", "telemetry"}
    assert result["audio_url"].startswith("https://")
    assert len(storage.uploads) == 1
    assert storage.uploads[0][1] == "audio/wav"
    assert storage.uploads[0][0] == executor.audio
    _output_validator().validate(result)


def test_large_audio_without_storage_is_visible_error(setup) -> None:
    config, manifest, _models_root = setup
    handler = build_handler(_small_inline_config(config), FakeExecutor(wav_bytes(2.0)), manifest)
    with pytest.raises(WorkerError) as error:
        _run(handler(_job()))
    assert error.value.code == "output_storage_missing"


# --- Пример контракта end-to-end и импорт без runpod -------------------------


def test_contract_example_end_to_end(setup) -> None:
    payload = load_json(contract_dir() / "examples" / "request-sfx.json")["input"]
    handler, _ = _handler(setup, FakeExecutor(wav_bytes(2.0), elapsed_seconds=6.4))
    result = _run(handler({"id": "example", "input": payload}))

    _output_validator().validate(result)
    telemetry = result["telemetry"]
    assert telemetry["seed"] == payload["parameter"]["seed"]
    assert telemetry["model_hash"] == "sha256:" + model_sha("stable_audio_3_small_sfx.safetensors")
    assert telemetry["duration_seconds"] == pytest.approx(2.0, abs=1e-3)


def test_import_without_runpod() -> None:
    sys.modules.pop("runpod", None)
    import audio_worker  # noqa: F401
    import audio_worker.handler  # noqa: F401

    assert "runpod" not in sys.modules
