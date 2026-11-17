# Changelog

Изменения воркера `audio-serverless` (стенд `audio`). Формат —
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии —
[SemVer](https://semver.org/lang/ru/).

## [0.2.0] — 2026-11-17

Релиз для публикации на RunPod Hub: метаданные листинга и build-тест, плюс
health-проверка без графа и модели.

### Добавлено

- **RunPod Hub** (`.runpod/hub.json`, `.runpod/tests.json`): листинг (GPU-пул
  `AMPERE_48,ADA_24`, CUDA 13.0–13.4, 40 GB container disk, env-переменные) и
  build-тест `action=health`.
- **Health** (`src/audio_worker/health.py`, contract §3): вход
  `{"action": "health"}` → `{status, comfyui, volume_mounted}`. Проверяются
  только наличие `VOLUME_ROOT` и короткий `GET /system_stats`; модель и граф не
  загружаются, ошибки ComfyUI health не валят. Статусы
  `ok`/`starting`/`degraded`, `comfyui` — `ready`/`booting`/`unavailable`.
- **Клиент ComfyUI**: `system_stats()` (одиночный probe с таймаутом) и
  `last_error` — причина последнего отказа для `booting`/`unavailable`.
- **Схема входа** допускает `action: health` без `prompt`/`parameter`
  (аддитивно в `audio-serverless/v1`); примеры `health.request.json`/
  `health.response.json`, health-тесты и contract-тесты на health.
- Версия `0.2.0` (pyproject, Dockerfile, `__version__`, Hub-метаданные);
  README — раздел Health и шаг публикации через Hub.

### Не менялось

- Контракт генерации `audio-serverless/v1`, телеметрия, pinned-манифест и
  ComfyUI-база. Живой entrypoint и проверка звукового стека — прежние
  live-шаги (`TODO(live)`).

## [0.1.0] — 2026-11-17

Первый релиз: контракт `audio-serverless/v1` (готовый ComfyUI-граф → аудиофайл
+ телеметрия), исполнитель для SFX/музыки и pinned-манифест тома. Живой прогон
на стенде не выполнялся: `TODO(live)` в `src/audio_worker/comfy_client.py` и
`src/audio_worker/audio.py`, entrypoint подъёма ComfyUI — в README
«Remaining live steps».

### Добавлено

- **Контракт `audio-serverless/v1`** (`contract/`): вход `{prompt
  (ComfyUI API graph), parameter {kind, duration_seconds, seed, prompt_hash,
  model_slug}, assets}`, выход `{audio_b64 | audio_url, telemetry}`;
  JSON-схемы входа/выхода, примеры SFX и музыки, документ контракта с
  лимитами (`sfx`/`layer` ≤ 30 с, `music` ≤ 90 с) и кодами ошибок.
- **Исполнитель ComfyUI** (`src/audio_worker/`): готовый граф уходит в
  `/prompt` без правок, результат читается из `/history` и `/view`; воркер не
  выбирает модель (поля выбора отвергаются `input_invalid`), не подставляет
  параметры и отдаёт ровно один файл. Пустой или битый звук — ошибка
  (`output_empty`/`output_invalid`), а не успех.
- **Телеметрия из фактов исполнения**: `engine_version` и `license` — из
  pinned-строки манифеста исполненного файла, `model_hash` — измеренный
  sha256 (расхождение с манифестом — `provision_hash_mismatch`), `seed` — из
  узла сэмплера графа, `duration_seconds`/`sample_rate`/`channels` — из
  файла, `gpu_seconds` — от `POST /prompt` до готового `/history`.
- **Выдача результата**: base64 с порогом `OUTPUT_INLINE_MAX_MB`, presigned
  `audio_url` в S3 для крупных результатов (`OUTPUT_URL_TTL_SECONDS`).
- **Том и веса**: `models-manifest.txt` с pinned URL, HF-ревизией и sha256 для
  Stable Audio 3.0 Small SFX + text encoder (Stability AI Community License) и
  ACE-Step 1.5 turbo AIO (MIT, ComfyUI-репак); ~13.5 GB, том ≥ 25 GB.
  `populate-volume.sh` отказывает URL без `/resolve/<40-hex-commit>/`,
  проверяет sha256, идемпотентен и докачивает `.tmp` после обрыва.
- **Образ**: слой на pinned ComfyUI v0.44.0 CUDA 13.0 base (digest), без весов
  и секретов; ComfyUI слушает только loopback, `EXPOSE` нет; endpoint —
  `workers.max=1`, timeout ≥ `JOB_TIMEOUT_SECONDS` (900 с).
- **Тесты**: 80 тестов без GPU, сети и ComfyUI (схемы и примеры контракта,
  happy path WAV/FLAC и факты seed/model из исполнения, пустой/битый звук,
  «воркер не выбирает модель», лимиты длительности, манифест и storage);
  `ruff check` — чисто.

### Ограничения релиза

- `TODO(live)`: набор ComfyUI-нод SA3/ACE-Step и их `class_type`, ключ `audio`
  в `/history.outputs`, формат сохранения (WAV; FLAC парсится), `/interrupt` и
  cold start — не проверены на живом стенде.
- Нет entrypoint'а, поднимающего ComfyUI перед handler'ом: образ собирается,
  но live endpoint без него отвечает `comfy_timeout`; шаблон —
  `image-serverless` (`docker/entrypoint.sh`, `scripts/wait-comfy.sh`).
- Шаблоны графов (`audio_models.graph.template`) и каталог живут в сервисе
  `video_gen`; имена файлов должны совпадать с манифестом.
- Endpoint/том не созданы; замеры GPU-секунд и пилот 18 SFX (план §11) не
  выполнены — внешний блокер приёмки.
