# Audio Serverless — ComfyUI worker for SFX / music

RunPod serverless worker for the video_gen **audio** stand (`audio` modality):
it runs ComfyUI on one 24–48 GB GPU, executes a **prepared ComfyUI API audio
graph** built by the video_gen catalog and returns the produced audio plus
execution-fact telemetry. It does not choose a model, does not build or edit the
graph, ships no weights and keeps ComfyUI on loopback only. Contract:
**`audio-serverless/v1`** — [`contract/worker-contract.md`](contract/worker-contract.md).
Platform plan: `docs/tasks/2026-11-16-audio-sfx-tts.md` §4.1/§7.1 in the
`video_gen` repository (separate repository: `/audio-serverless/` is in the
parent `.gitignore`).

## Models on the stand

| Model | Use | Volume files | License |
|---|---|---|---|
| Stable Audio 3.0 Small SFX | SFX / one-shots / layers | `checkpoints/stable_audio_3_small_sfx.safetensors` + `text_encoders/t5gemma_b_b_ul2.safetensors` | Stability AI Community License (commercial outputs while yearly revenue < $1M; above — Enterprise) |
| ACE-Step 1.5 turbo AIO | music 15–90 s | `checkpoints/ace_step_1.5_turbo_aio.safetensors` | MIT (original ACE-Step weights, ComfyUI repack) |

Pinned URL/revision/sha256 and telemetry version/license values:
[`models-manifest.txt`](models-manifest.txt). MOSS SoundEffect v2 is a catalog
reserve and needs no worker change (add its pinned files + a graph template).
Stable Audio 3 music can be added later the same way.

The worker reports the license of the **executed** model file; the service
license gate (`commercial_only`, `audio_models`) decides whether the model may
be used at all — see the platform plan §4.3.

## Deploy

1. **Storage first.** Create a network volume (≥ 25 GB) in a datacenter that
   offers 24 GB+ GPUs (L4, A5000, RTX A6000/A40, 4090). The pinned set is
   ~13.5 GB; ComfyUI outputs/temp and future models need headroom.
2. **Volume auto-populates on first boot.** `docker/entrypoint.sh` runs
   [`populate-volume.sh`](populate-volume.sh) with
   `VOLUME_AUTOPOPULATE=true` (default): missing files from
   `models-manifest.txt` are downloaded to `/runpod-volume/models` and every
   sha256 is verified; unpinned URLs/`main` are refused before GPU work. Set
   `VOLUME_AUTOPOPULATE=false` to prepare the volume manually from a CPU pod
   (`MODELS_ROOT=/runpod-volume/models ./populate-volume.sh`).
3. **Build and push the image** (≤ 8 GB without weights; basic GPU). The
   Dockerfile builds on the pinned ComfyUI CUDA base — see
   [Dockerfile](Dockerfile). Do not bake weights into the image. The base is a
   pod image with `ENTRYPOINT /start.sh` and no `python` in PATH (ComfyUI lives
   in `/workspace/runpod-slim/.venv-cu130`); the worker layer symlinks `python`
   to the base `python3` (the interpreter the worker deps are installed into)
   and smoke-imports the handler at build time, so `python handler.py` works in
   the Hub test pod and on a live endpoint.
4. **Publish on RunPod Hub (optional).** The Hub listing metadata is
   [`.runpod/hub.json`](.runpod/hub.json) (GPU pools `AMPERE_48,AMPERE_24,ADA_24` —
   A6000/A40 48 GB primary, 3090/4090 cheap fallback; host CUDA `13.2`/`13.0` —
   the image is built on CUDA 13.0, so the host driver must be 13.0+; only
   versions present in the RunPod inventory are listed, speculative `13.1`/
   `13.3`/`13.4` never matched a machine; 40 GB container disk) and
   the build test is [`.runpod/tests.json`](.runpod/tests.json) — it runs the
   health action on `NVIDIA H100 80GB HBM3` (CUDA 13.2). The build test needs
   any GPU pod and the platform's 24–48 GB classes are frequently out of
   stock, while the 80 GB class had capacity across nine data centers when
   checked: the test pod is short-lived and the health action does not
   initialize CUDA, so it only verifies the handler/contract wiring. Listing
   pools keep the practical tiers (`AMPERE_48,AMPERE_24,ADA_24,ADA_48_PRO,
   BLACKWELL_32`) and add `ADA_80_PRO` so the test GPU is inside them.
5. **Create the serverless endpoint** for the `audio` stand: endpoint type
   `QUEUE`, **`workers.max = 1`** (batch rule from the plan; a different value is
   stopped with `endpoint_workers_max`), idle timeout 60–120 s, FlashBoot,
   endpoint timeout ≥ `JOB_TIMEOUT_SECONDS` (900 s; recommend 1200 s). One warm
   worker that already loaded the model beats several cold ones.
6. **Smoke-test** with the health action (see [Health](#health)); RunPod Hub runs
   the same request on build. The generation smoke test is
   `contract/examples/request-sfx.json` (the graph is illustrative; the real
   template lives in the `audio_models` catalog).

## Job input / output

```json
{
  "input": {
    "prompt": {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "stable_audio_3_small_sfx.safetensors"}}},
    "parameter": {"kind": "sfx", "duration_seconds": 2.0, "seed": 20261116, "prompt_hash": "<hash>", "model_slug": "stable-audio-3-small-sfx"},
    "assets": {}
  },
  "policy": {"executionTimeout": 180000, "ttl": 3600000}
}
```

| Field | Description |
|---|---|
| `prompt` | Complete ComfyUI API graph, submitted unchanged. Generated by the service from `audio_models.graph.template`; must end in a save-audio node writing **WAV** (or FLAC). |
| `parameter` | Snapshot (`kind`, `duration_seconds`, `seed`, `prompt_hash`, `model_slug`). Tracing/limits only: the worker never picks a model from it and never substitutes its values for telemetry. |
| `assets` | Must be `{}` — the audio contract has no input assets. |
| `policy` | Optional RunPod policy; the worker does not read it. |

Result: exactly one of `audio_b64` / `audio_url` plus `telemetry`
(`engine_version`, `model_hash`, `seed`, `duration_seconds`, `sample_rate`,
`channels`, `gpu_seconds`, `license`). Empty/broken audio is an error, not a
success; telemetry facts come from the executed graph, the produced file and the
pinned manifest. Full details and error codes: [contract](contract/worker-contract.md).
Examples: [`contract/examples/`](contract/examples/).

## Health

Request (`POST /run`): `{"input": {"action": "health"}}`

```json
{"status": "ok", "comfyui": "ready", "volume_mounted": true}
```

- `status`: `ok` (ComfyUI ready and the volume mounted), `starting` (ComfyUI
  booting, volume mounted; normal transient state) or `degraded` (volume missing
  and/or ComfyUI unavailable);
- `comfyui`: `ready`, `booting` or `unavailable` — a short `GET /system_stats`
  probe;
- `volume_mounted`: whether `VOLUME_ROOT` exists.

Health never loads a model or runs a graph: it is safe as an endpoint smoke test
and for diagnostics. RunPod Hub runs it on build
([`.runpod/tests.json`](.runpod/tests.json)).

## Environment

Secrets only in the endpoint env; never in the image/repository/logs.

| Variable | Default | Purpose |
|---|---|---|
| `COMFY_URL` | `http://127.0.0.1:8188` | Local ComfyUI (loopback only). |
| `JOB_TIMEOUT_SECONDS` | `900` | Graph run limit inside the handler. |
| `HEALTH_TIMEOUT_SECONDS` | `10` | Single `GET /system_stats` timeout for the health action. |
| `MODELS_ROOT` / `MODELS_MANIFEST` | `/runpod-volume/models` / `/opt/audio-worker/models-manifest.txt` | Volume model root and pinned manifest. |
| `OUTPUT_INLINE_MAX_MB` / `OUTPUT_INLINE_BUDGET_MB` / `OUTPUT_URL_TTL_SECONDS` | `14` / `20` / `86400` | base64 vs presigned `audio_url` thresholds. |
| `S3_*` (`S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`, `S3_SECURE`) | — | Storage for large results. |

## Tests

```bash
python -m pytest            # 0 GPU, no network, no runpod, no ComfyUI
```

The suite covers the contract schemas and examples, handler happy path
(WAV/FLAC facts, seed/model facts from execution), empty/broken output errors,
"the worker does not choose a model" (strict input schema + graph/telemetry
facts), duration limits, model manifest parsing/pinning, storage delivery, and
health statuses (volume and ComfyUI probe, no graph).
Dependencies for a local run: `pytest`, `jsonschema` (see `pyproject.toml`).
`.runpod/` holds the RunPod Hub listing (`hub.json`) and the build test
(`tests.json`).

## Remaining live steps (not in this repository yet)

1. **Verify the audio ComfyUI stack on the stand** (`TODO(live)` in
   `src/audio_worker/comfy_client.py` and `audio.py`): Stable Audio 3 / ACE-Step
   node packages and their exact `class_type`s, the `audio` key in
   `/history.outputs`, save format (contract requires WAV; FLAC is parsed),
   `/interrupt` on cancellation, and cold-start time vs `policy.executionTimeout`.
   Entrypoint (`docker/entrypoint.sh`) and ComfyUI boot from the baked base are
   implemented in this repository but **not yet published/verified on the stand**.
2. **Catalog `model_files[]`** names exactly matching this manifest (graph
   templates/revision/hash already in the service seeds).
3. **Measure** warm/cold start, GPU seconds per SFX/music, and the real
   base64/URL threshold on the stand; record `cost.gpu_seconds`.
4. **Pilot go/no-go** on 18 catalog SFX names before the stand is enabled for
   authors (plan §11).
