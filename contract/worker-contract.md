# Worker contract — audio-serverless/v1

This document is the source of truth for the job input, output and errors of the
worker in this repository. The contract is versioned separately from the worker
release: an incompatible change is a new contract version (`v2`). The platform plan
is `docs/tasks/2026-11-16-audio-sfx-tts.md` §4.1/§7.1 in the `video_gen` repository.

The worker executes a **prepared ComfyUI API audio graph** on a local ComfyUI
instance and returns the produced audio plus execution-fact telemetry. It **never
builds, edits or selects a model**: the application renders the graph from the
`audio_models` catalog template, substitutes prompt/duration/seed and sends the
finished API graph. An empty or unreadable audio file is never a successful
result. JSON Schemas and examples:
[`input.schema.json`](input.schema.json), [`output.schema.json`](output.schema.json),
[`examples/`](examples/).

## 1. Job input

`POST {RUNPOD_SERVERLESS_API_BASE}/v2/{endpoint_id}/run`

```json
{
  "input": {
    "prompt": {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "stable_audio_3_small_sfx.safetensors"}}},
    "parameter": {"kind": "sfx", "duration_seconds": 2.0, "seed": 20261116, "prompt_hash": "<sha256>", "model_slug": "stable-audio-3-small-sfx"},
    "assets": {}
  },
  "policy": {"executionTimeout": 180000, "ttl": 3600000}
}
```

| Field | Meaning |
|---|---|
| `prompt` | Prepared ComfyUI API graph (`node id -> {class_type, inputs}`), submitted unchanged. Required. |
| `parameter.kind` | `sfx` / `music` / `layer`; drives the duration limit and the output category on the service side. |
| `parameter.duration_seconds` | Snapshot of the requested duration. The worker **does not** derive telemetry from it: `duration_seconds` in the output is measured from the produced file. |
| `parameter.seed` | Snapshot of the seed rendered into the graph (tracing only). The reported seed is read from the executed sampler node. |
| `parameter.prompt_hash` | Application prompt/template hash snapshot; tracing only. |
| `parameter.model_slug` | Application catalog slug snapshot; tracing only. **The worker does not choose or look up a model by this field** — see §1.2. |
| `assets` | Must be `{}` for this contract: there are no input audio assets. |
| `policy` | Optional RunPod policy (`executionTimeout`, `ttl`). The worker does not read it; RunPod applies it before the handler runs. |

`input` is strict (`additionalProperties: false`): an unknown field such as
`model`, `model_slug` at the input level or `steps` is rejected with
`input_invalid`, so the worker can never receive a model choice instead of the
prepared graph.

### 1.1. Duration limits

| `parameter.kind` | Limit |
|---|---|
| `sfx`, `layer` | `duration_seconds` &le; 30 |
| `music` | `duration_seconds` &le; 90 |

The service validates the limit before the GPU run (plan §7.1); the worker
enforces the same limit as a second gate and fails with
`duration_limit_exceeded` before submitting the graph. Even within the limit, a
result that does not fit the response budget is delivered as `audio_url`
(§2.1).

### 1.2. The worker never chooses a model

The executed model is whatever the submitted graph loads. The worker reads the
model file name from the executed graph loader node (§2.2), matches it against
the pinned [`models-manifest.txt`](../models-manifest.txt), verifies the file
sha256 and reports `engine_version` / `model_hash` / `license` for **that** file.
`parameter.model_slug` is never used for execution or telemetry; the service
compares the returned facts with its snapshot and stops the step on a deviation
(`docs/tasks/2026-11-16-audio-sfx-tts.md` §4.6).

## 2. Job output

The handler returns one object that RunPod places in `output` of the status
response.

```json
{
  "audio_b64": "<base64 wav/flac>",
  "telemetry": {
    "engine_version": "stable-audio-3-small-sfx@2026-05",
    "model_hash": "sha256:<64 hex>",
    "seed": 20261116,
    "duration_seconds": 2.0,
    "sample_rate": 48000,
    "channels": 2,
    "gpu_seconds": 6.4,
    "license": "Stability-AI-Community"
  }
}
```

### 2.1. Audio delivery

Exactly one of `audio_b64` or `audio_url` is present. Results whose raw size is
within `OUTPUT_INLINE_MAX_MB` **and** whose base64 payload is within
`OUTPUT_INLINE_BUDGET_MB` are returned inline; larger results go to
S3-compatible storage as a presigned `audio_url` with lifetime
`OUTPUT_URL_TTL_SECONDS`. If a result exceeds the inline limits and no storage is
configured, the job fails with `output_storage_missing` — a silent truncation is
never a success.

The produced file must be a **WAV or FLAC** audio file. `mp3`/`opus` output is
`output_invalid` until decoding is implemented (a `TODO(live)` in
`src/audio_worker/audio.py`); the application's `audio_models.graph.template`
must end in a save-audio node configured for `wav` (`SaveAudioAdvanced` with
`format: "wav"`, or `SaveAudio`).

### 2.2. Telemetry

`telemetry` is required and complete; every value is an execution fact:

| Field | Measured from |
|---|---|
| `engine_version` | The pinned `version` column of the manifest row for the executed model file. |
| `model_hash` | sha256 of the executed model weight file on the volume, verified equal to the pinned manifest sha256 (`provision_hash_mismatch` otherwise). |
| `seed` | `KSampler.seed` / `KSamplerAdvanced.seed`; for `SamplerCustom*` — the linked `RandomNoise.noise_seed`. |
| `duration_seconds`, `sample_rate`, `channels` | Header of the produced WAV/FLAC file. |
| `gpu_seconds` | Wall clock from `POST /prompt` to the completed `/history` entry and output fetch. |
| `license` | The pinned `license` column of the manifest row for the executed model file. |

Rules:

- If a fact cannot be measured the job fails with `telemetry_missing`; if the
  executed graph contains several model loaders or disagreeing sampler seeds the
  job fails with `telemetry_ambiguous`. Expected values are never substituted.
- `model_hash` is prefixed with `sha256:`.
- A zero-byte audio file is `output_empty`; a non-WAV/FLAC or unreadable file is
  `output_invalid`. Neither is a success.
- The contract has no `gpu_cc`/`gpu_class` fields: the audio telemetry is the
  list above; RunPod/provider facts live in the RunPod job status.

## 3. Health

Request (`POST /run`):

```json
{"input": {"action": "health"}}
```

Response:

```json
{"status": "ok", "comfyui": "ready", "volume_mounted": true}
```

- `status`: `ok` (ComfyUI ready and the network volume mounted), `starting`
  (ComfyUI booting, volume mounted; normal transient state) or `degraded` (volume
  missing and/or ComfyUI unavailable).
- `comfyui`: `ready`, `booting` or `unavailable` — a short `GET /system_stats`
  probe; connection/timeout failures while booting are `booting`, other failures
  are `unavailable`.
- `volume_mounted`: whether `VOLUME_ROOT` exists.
- Health never loads a model or a graph: with `action: health` the `prompt` and
  `parameter` fields are not required, so it is safe as an endpoint smoke test
  and for diagnostics. RunPod Hub runs it on build
  ([`.runpod/tests.json`](../.runpod/tests.json)).

## 4. Graph constraints the application must respect

The worker is model-agnostic: adding an audio model means adding files to the
volume manifest and a graph template to the `audio_models` catalog, not changing
this repository. The submitted graph must:

1. be a complete ComfyUI **API-format** graph with no `{{...}}` placeholders
   left and end in a save-audio node that writes **WAV or FLAC**;
2. load the model with one of `CheckpointLoaderSimple`, `CheckpointLoader`,
   `UNETLoader`, `UnetLoaderGGUF`, `UnetLoaderGGUFAdvanced` (input
   `ckpt_name`/`unet_name`) — exactly one such main model per graph;
3. set the seed on `KSampler`/`KSamplerAdvanced` (`seed`) or on a linked
   `RandomNoise` (`noise_seed`) for `SamplerCustom*`;
4. reference model file names exactly as they are pinned in
   [`models-manifest.txt`](../models-manifest.txt) and placed on the volume by
   `populate-volume.sh`.

Models on the stand: **Stable Audio 3.0 Small SFX** (SFX; Stability AI
Community License) and **ACE-Step 1.5 turbo AIO** (music; MIT repackaged for
ComfyUI). MOSS SoundEffect v2 is a catalog reserve and needs no worker change.

## 5. Errors and cancellation

Any handler error fails the RunPod job (`FAILED`); error codes are stable
lowercase identifiers:

| Group | Codes |
|---|---|
| Input/config | `input_invalid`, `config_invalid`, `duration_limit_exceeded` |
| Model manifest | `model_file_missing`, `provision_hash_mismatch` |
| Graph/telemetry | `telemetry_missing`, `telemetry_ambiguous` |
| ComfyUI | `comfy_submit_failed`, `comfy_execution_failed`, `comfy_timeout`, `comfy_output_missing` |
| Output | `output_empty`, `output_invalid`, `output_storage_missing`, `output_storage_failed` |

- `POST /v2/{endpoint_id}/cancel/{run_id}` interrupts the running graph in
  ComfyUI before the job terminates (`asyncio.CancelledError` → `POST
  /interrupt`). `JOB_TIMEOUT_SECONDS` (default 900) bounds the graph run inside
  the handler; the endpoint timeout and `policy.executionTimeout` must be
  greater than or equal to it.
- The handler validates its own output against
  [`output.schema.json`](output.schema.json) before returning; a schema
  violation is `output_invalid`.

## 6. Environment

The worker reads its settings from endpoint environment variables. Invalid values
fail the job with `config_invalid` and the variable name.

| Variable | Default | Purpose |
|---|---|---|
| `COMFY_URL` | `http://127.0.0.1:8188` | Local ComfyUI address; loopback only. |
| `JOB_TIMEOUT_SECONDS` | `900` | Maximum graph run time inside the handler. |
| `COMFY_POLL_INITIAL_SECONDS` / `COMFY_POLL_MAX_SECONDS` / `COMFY_REQUEST_TIMEOUT_SECONDS` | `0.5` / `3` / `60` | `/history` polling backoff and per-request HTTP timeout. |
| `VOLUME_ROOT` / `MODELS_ROOT` | `/runpod-volume` / `${VOLUME_ROOT}/models` | Volume mount point and root of model weights. |
| `MODELS_MANIFEST` | `/opt/audio-worker/models-manifest.txt` | Pinned model manifest shipped in the image. |
| `OUTPUT_INLINE_MAX_MB` / `OUTPUT_INLINE_BUDGET_MB` | `14` / `20` | Raw inline limit and encoded-payload budget (§2.1). |
| `OUTPUT_URL_TTL_SECONDS` | `86400` | Lifetime of the presigned `audio_url`. |
| `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`, `S3_SECURE` | — / `us-east-1` / auto | S3-compatible storage for large results; bucket alias `OUTPUT_BUCKET`. |
| `CONTRACT_DIR` | next to the repository/image (`contract/`) | JSON Schemas used by the handler. |

`S3_ACCESS_KEY` and `S3_SECRET_KEY` are secrets: set them only in the endpoint
environment, never in the image, the repository or logs. Weights are never in
the image: they live on the network volume (`models-manifest.txt`,
`populate-volume.sh`). ComfyUI itself is booted by the image entrypoint (see
README "Remaining live steps") and is never exposed.

## 7. Version

Current contract version: **`audio-serverless/v1`**. Input and output are
additive within a minor version. An incompatible change requires a new contract
version and a matching update of the schemas, examples and this document.
