"""Схемы контракта: примеры валидны, нарушения видны, схемы — draft 2020-12."""

from __future__ import annotations

import copy

import pytest
from helpers import MANIFEST_MODELS, contract_dir, load_json
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


@pytest.fixture(scope="module")
def input_schema() -> dict:
    return load_json(contract_dir() / "input.schema.json")


@pytest.fixture(scope="module")
def output_schema() -> dict:
    return load_json(contract_dir() / "output.schema.json")


@pytest.fixture(scope="module")
def input_validator(input_schema: dict) -> Draft202012Validator:
    Draft202012Validator.check_schema(input_schema)
    return Draft202012Validator(input_schema)


@pytest.fixture(scope="module")
def output_validator(output_schema: dict) -> Draft202012Validator:
    Draft202012Validator.check_schema(output_schema)
    return Draft202012Validator(output_schema)


def _assert_invalid(validator: Draft202012Validator, payload: object) -> None:
    with pytest.raises(ValidationError):
        validator.validate(payload)


def _sfx_input() -> dict:
    return copy.deepcopy(load_json(contract_dir() / "examples" / "request-sfx.json")["input"])


# --- Примеры -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["request-sfx.json", "request-music.json", "health.request.json"])
def test_input_examples_are_valid(input_validator: Draft202012Validator, name: str) -> None:
    input_validator.validate(load_json(contract_dir() / "examples" / name))


def test_health_allows_no_prompt_and_parameter(input_validator: Draft202012Validator) -> None:
    input_validator.validate({"input": {"action": "health"}})


def test_health_response_shape() -> None:
    payload = load_json(contract_dir() / "examples" / "health.response.json")
    assert payload["status"] in {"ok", "starting", "degraded"}
    assert payload["comfyui"] in {"ready", "booting", "unavailable"}
    assert isinstance(payload["volume_mounted"], bool)


@pytest.mark.parametrize("name", ["response.json", "response-url.json"])
def test_output_examples_are_valid(output_validator: Draft202012Validator, name: str) -> None:
    output_validator.validate(load_json(contract_dir() / "examples" / name))


# --- Вход --------------------------------------------------------------------


def test_prompt_and_parameter_are_required(input_validator: Draft202012Validator) -> None:
    _assert_invalid(input_validator, {"input": {"parameter": _sfx_input()["parameter"]}})
    _assert_invalid(input_validator, {"input": {"prompt": _sfx_input()["prompt"]}})
    _assert_invalid(input_validator, {"input": {}})


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "video"),
        ("kind", None),
        ("duration_seconds", 0),
        ("duration_seconds", -1),
        ("seed", -1),
        ("seed", 1.5),
        ("prompt_hash", ""),
        ("model_slug", ""),
    ],
)
def test_parameter_field_validation(
    input_validator: Draft202012Validator, field: str, value: object
) -> None:
    payload = _sfx_input()
    payload["parameter"][field] = value
    _assert_invalid(input_validator, {"input": payload})


def test_the_worker_cannot_be_given_a_model_choice(input_validator: Draft202012Validator) -> None:
    """Модель выбирает сервис графом; поле выбора модели во входе невалидно."""

    payload = _sfx_input()
    payload["model"] = "ace-step-1-5"
    _assert_invalid(input_validator, {"input": payload})
    # parameter — снимок для сверки: дополнительные tracing-поля допустимы, но
    # выбора модели в нём нет; выбор задаёт только граф.
    payload = _sfx_input()
    payload["parameter"]["model"] = {"slug": "ace-step-1-5"}
    input_validator.validate({"input": payload})
    # parameter — снимок для сверки: дополнительные tracing-поля допустимы, но
    # выбора модели в нём нет; выбор задаёт только граф.
    payload = _sfx_input()
    payload["parameter"]["unet_name"] = "other.safetensors"
    input_validator.validate({"input": payload})


def test_assets_must_be_empty(input_validator: Draft202012Validator) -> None:
    payload = _sfx_input()
    payload["assets"] = {"base.wav": "https://files.example/base.wav"}
    _assert_invalid(input_validator, {"input": payload})
    payload["assets"] = {}
    input_validator.validate({"input": payload})


def test_seed_may_be_null(input_validator: Draft202012Validator) -> None:
    payload = _sfx_input()
    payload["parameter"]["seed"] = None
    input_validator.validate({"input": payload})


def test_policy_is_accepted_and_optional(input_validator: Draft202012Validator) -> None:
    payload = _sfx_input()
    input_validator.validate({"input": payload, "policy": {"executionTimeout": 120000, "ttl": 3600000}})


# --- Выход -------------------------------------------------------------------


VALID_TELEMETRY = {
    "engine_version": "stable-audio-3-small-sfx@2026-05",
    "model_hash": "sha256:" + "e" * 64,
    "seed": 20261116,
    "duration_seconds": 2.0,
    "sample_rate": 48000,
    "channels": 2,
    "gpu_seconds": 6.4,
    "license": "Stability-AI-Community",
}


def _output(**overrides: object) -> dict:
    payload = {"audio_b64": "aGVsbG8=", "telemetry": copy.deepcopy(VALID_TELEMETRY)}
    payload.update(overrides)
    return payload


def test_valid_outputs(output_validator: Draft202012Validator) -> None:
    output_validator.validate(_output())
    payload = {
        "audio_url": "https://storage.example/audio/x.wav?signature=1",
        "telemetry": copy.deepcopy(VALID_TELEMETRY),
    }
    output_validator.validate(payload)


def test_exactly_one_audio_delivery(output_validator: Draft202012Validator) -> None:
    _assert_invalid(
        output_validator,
        _output(audio_url="https://storage.example/audio/x.wav"),
    )
    _assert_invalid(output_validator, {"telemetry": copy.deepcopy(VALID_TELEMETRY)})


def test_unknown_output_field_is_rejected(output_validator: Draft202012Validator) -> None:
    _assert_invalid(output_validator, _output(image="aGVsbG8="))


@pytest.mark.parametrize("missing", sorted(VALID_TELEMETRY))
def test_telemetry_required_fields(output_validator: Draft202012Validator, missing: str) -> None:
    telemetry = copy.deepcopy(VALID_TELEMETRY)
    telemetry.pop(missing)
    _assert_invalid(output_validator, {"audio_b64": "aGVsbG8=", "telemetry": telemetry})


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_seconds", 0),
        ("duration_seconds", -2),
        ("sample_rate", 0),
        ("channels", 0),
        ("channels", 9),
        ("gpu_seconds", 0),
        ("seed", -1),
        ("engine_version", ""),
        ("model_hash", "abc"),
        ("model_hash", "sha256:ABCD"),
        ("license", ""),
    ],
)
def test_telemetry_bad_values(
    output_validator: Draft202012Validator, field: str, value: object
) -> None:
    telemetry = copy.deepcopy(VALID_TELEMETRY)
    telemetry[field] = value
    _assert_invalid(output_validator, {"audio_b64": "aGVsbG8=", "telemetry": telemetry})


# --- Примеры связаны с манифестом -------------------------------------------


def test_example_graphs_reference_manifest_files() -> None:
    """Графы примеров ссылаются на файлы, которые populate-volume.sh кладёт на том."""

    for name in ("request-sfx.json", "request-music.json"):
        payload = load_json(contract_dir() / "examples" / name)
        for node in payload["input"]["prompt"].values():
            inputs = node.get("inputs", {})
            for key in ("ckpt_name", "unet_name", "clip_name", "vae_name"):
                value = inputs.get(key)
                if isinstance(value, str):
                    assert value in MANIFEST_MODELS, f"{name}: {value} нет в models-manifest.txt"
