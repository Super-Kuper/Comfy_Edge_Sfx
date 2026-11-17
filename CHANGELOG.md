# Changelog

Изменения воркера `audio-serverless` (стенд `audio`). Формат —
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии —
[SemVer](https://semver.org/lang/ru/).

## [0.2.6] — 2026-11-17

Живой entrypoint: ComfyUI, авто-популяция тома, запуск handler.

### Добавлено

- **`docker/entrypoint.sh`:** pinned-веса на том (`VOLUME_AUTOPOPULATE=true` по
  умолчанию, `populate-volume.sh` + sha256 из манифеста) → `extra_model_paths.yaml`
  → ComfyUI на `127.0.0.1:8188` → ожидание `/system_stats`
  (`COMFY_BOOT_TIMEOUT_SECONDS`, по умолчанию 600) → handler; SIGTERM/SIGINT
  пробрасываются handler'у, ComfyUI гасится при выходе. Ошибка автоскачивания
  не валит контейнер: health доступен, job падает видимым `model_file_missing`.
- **`docker/install-comfy-packages.sh`:** материализация baked-раскладки ComfyUI
  v0.44.0 и venv на этапе сборки (как в `image-serverless`), проверка core-нод
  audio.
- **`scripts/wait-comfy.sh`**, **`docker/extra_model_paths.yaml`** (checkpoints,
  text_encoders и общие каталоги тома).
- Hub env: `VOLUME_AUTOPOPULATE` (default `true`).

### Изменено

- Dockerfile: ENV ComfyUI/тома, `rsync`/`git`, `ENTRYPOINT` —
  `docker/entrypoint.sh` (прежний `python handler.py` убран).

### Проверка

`pytest` — 89 passed; `shellcheck` — только прежние SC2034 в
`populate-volume.sh`; stub-сборка на `python:3.12-slim` зелёная. Живой прогон на
стенде не выполнялся: нужна пересборка/публикация образа и GPU.

## [0.2.5] — 2026-11-17

Hub-билд: test pod на GPU с фактическим стоком (H100 80GB).

### Исправлено

- **Причина:** CUDA-фильтр был корректен (`13.2` — единственная реально
  доступная версия, `13.4` — 0 машин), но 24–48-ГБ классы снова остались без
  стока: прямой `POST /v2/pods` показал, что в момент сборки ставятся только
  `H100 80GB`/`H200`/`B200`/`RTX PRO 6000` (2/2), а `4090`/`A6000`/`3090`/
  `L40S`/`RTX PRO 4500` — нет.
- **`audio`:** test pod — `NVIDIA H100 80GB HBM3` (CUDA 13.2; 9 ДЦ, сток
  подтверждён постановкой Pod'ов 2/2). Health-тест не инициализирует CUDA и
  живёт секунды; в пулы листинга добавлен `ADA_80_PRO` (H100), практические
  24–48-ГБ пулы сохранены.
- Версия `0.2.5`; контракт, обработчик и тесты не менялись.

## [0.2.4] — 2026-11-17

Hub-билд: test pod на GPU с фактическим стоком (нехватка 24-ГБ класса).

### Исправлено

- **Причина:** CUDA-конфиг был корректен, но 24-ГБ класс GPU стоял без
  стока: прямой `POST /v2/pods` для `RTX 4090`/`A6000`/`3090` с CUDA 13.2
  возвращал «no longer any instances available», и Hub отвечал
  `All returned supply constraints` (4090 → B300 MIG → RTX PRO 4500).
  Каталог подтверждает: `13.4` — 0 машин, `13.0` — 0 доступных, доступна
  только `13.2` (у 4090 — единственная), поэтому добавление `13.4` ничего бы
  не дало.
- **`audio`:** test pod — `NVIDIA RTX PRO 4500 Blackwell` (32 ГБ, CUDA 13.2;
  сток подтверждён реальной постановкой Pod'а); в пулах листинга добавлены
  `ADA_48_PRO` (L40S) и `BLACKWELL_32` (RTX PRO 4500), CUDA-список остался
  `13.2`/`13.0` (образ на CUDA 13.0, хост обязан быть ≥ 13.0).
- Версия `0.2.4`; контракт, обработчик, тесты и манифест не менялись.

## [0.2.3] — 2026-11-17

Hub-билд: контейнер падал на `python: not found`.

### Исправлено

- **Причина:** test pod поднялся на RTX 4090, но контейнер завершился с
  `sh: 1: python: not found`: в pod-базе `runpod/comfyui` (CUDA 13.0) имени
  `python` в PATH нет — ComfyUI живёт в `/workspace/runpod-slim/.venv-cu130`,
  venv не активирован, а дефолтный `ENTRYPOINT /start.sh` заменён нашим.
  TTS-образ на `python:3.12-slim` содержит `python`, поэтому собирался.
- **`audio`:** в Dockerfile `python` → `python3` (симлинк в `/usr/local/bin` на
  тот же интерпретатор, куда поставлены зависимости воркера) + smoke
  `python -c "import audio_worker.handler"` на этапе сборки. ENTRYPOINT
  `python /opt/audio-worker/handler.py` теперь резолвится, и Hub test pod
  может запустить handler.
- Версия `0.2.3`; контракт, обработчик, тесты и манифест не менялись.

## [0.2.2] — 2026-11-17

Hub-билд: test pod переведён на 24-ГБ класс GPU.

### Исправлено

- **Причина:** после исправления CUDA-списка (`13.2`/`13.0`) Hub всё равно
  вернул `All returned supply constraints`: test pod перебирал 48-ГБ класс
  `NVIDIA RTX A6000` → `A40` → `L40`, а у него низкий/нулевой сток (`LOW`, у
  `L40` — `NONE`).
- **`audio`:** test pod — `NVIDIA GeForce RTX 4090` (24 ГБ, CUDA `13.2`, сток
  `LOW`) — тот же класс, на котором собрался TTS-воркер. Тест выполняет только
  `action: health` (без инициализации CUDA), поэтому 24 ГБ достаточно;
  листинговые пулы `AMPERE_48,AMPERE_24,ADA_24` и требование host-CUDA ≥ 13.0
  для развёртывания CUDA 13.0-образа не менялись.
- Версия `0.2.2`; контракт, обработчик, тесты и манифест не менялись.

## [0.2.1] — 2026-11-17

Исправлен Hub-билд: `allowedCudaVersions` ссылался на версии CUDA, которых нет
на машинах RunPod.

### Исправлено

- **Причина:** RunPod Hub поднимает test pod по `.runpod/tests.json`; если
  `allowedCudaVersions` не совпадает ни с одной машиной, все попытки падают с
  `All returned supply constraints` (в логе TTS — L4/3090/A5000 и
  `12.8, 12.9, 13.0`). Каталог RunPod (`GET /v2/catalog/gpus`) показывает
  реальные host-CUDA: `13.2`, `13.0`, `12.6`, `12.4`; `12.8`, `12.9`, `13.1`,
  `13.3`, `13.4` отсутствуют, а у пула `AMPERE_24` нет стока GPU L4.
- **`audio`:** `13.0`–`13.4` → `13.2`, `13.0` (образ собран на CUDA 13.0, хост
  обязан быть ≥ 13.0); пулы `AMPERE_48,AMPERE_24,ADA_24` — A6000/3090/4090 с
  реальным стоком; test pod — NVIDIA RTX A6000 с CUDA 13.2.
- Версия `0.2.1`; контракт `audio-serverless/v1`, обработчик, тесты и манифест
  не менялись. Живой entrypoint — прежний `TODO(live)`.

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
