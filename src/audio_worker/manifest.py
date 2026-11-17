"""Pinned-манифест весов audio-стенда (`models-manifest.txt`).

Формат строки (tab-separated):

    <subdir>\t<filename>\t<URL>\t<sha256>\t<version>\t<license>

* URL обязан быть pinned на immutable commit Hugging Face
  (`/resolve/<40-hex-commit>/`); `main`/`master` запрещены.
* sha256 — хеш файла (LFS oid), 64 lowercase hex.
* version — версия движка/модели для телеметрии (``slug@YYYY-MM``).
* license — идентификатор лицензии для телеметрии (Community License, MIT, ...).

Файлы кладутся на том как ``<MODELS_ROOT>/<subdir>/<filename>`` — ровно тот
layout, который ComfyUI видит через ``extra_model_paths.yaml``. Веса в git не
хранятся; скачивание/проверка — ``populate-volume.sh``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkerError

PIN_RE = re.compile(r"/resolve/([0-9a-f]{40})/")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(r"^https://")

_CACHE: dict[tuple[str, int, int], str] = {}


@dataclass(frozen=True)
class ModelFile:
    """Одна pinned-запись манифеста."""

    subdir: str
    filename: str
    url: str
    sha256: str
    version: str
    license: str

    @property
    def relative_path(self) -> Path:
        return Path(self.subdir) / self.filename


def sha256_file(path: Path) -> str:
    """sha256 файла; результат кешируется по (путь, mtime, размер) в процессе."""

    stat = path.stat()
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _CACHE[key] = value
    return value


def parse_manifest(text: str) -> tuple[ModelFile, ...]:
    """Разобрать и провалидировать манифест; ошибка — ``config_invalid``."""

    entries: list[ModelFile] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) != 6:
            raise WorkerError(
                "config_invalid",
                f"models-manifest.txt:{number}: ожидалось 6 колонок "
                "<subdir>\\t<filename>\\t<URL>\\t<sha256>\\t<version>\\t<license>",
            )
        subdir, filename, url, sha256, version, license_id = (column.strip() for column in columns)
        if not subdir or "/" in subdir or subdir.startswith("."):
            raise WorkerError("config_invalid", f"models-manifest.txt:{number}: плохой subdir {subdir!r}")
        if not filename or Path(filename).name != filename or filename in {".", ".."}:
            raise WorkerError(
                "config_invalid", f"models-manifest.txt:{number}: имя файла без путей, получено {filename!r}"
            )
        if not URL_RE.match(url) or PIN_RE.search(url) is None or "/resolve/main/" in url:
            raise WorkerError(
                "config_invalid",
                f"models-manifest.txt:{number}: URL не запинен на commit: {url}",
            )
        if SHA_RE.fullmatch(sha256) is None:
            raise WorkerError("config_invalid", f"models-manifest.txt:{number}: sha256 не 64 hex")
        if not version or "@" not in version:
            raise WorkerError("config_invalid", f"models-manifest.txt:{number}: version без @: {version!r}")
        if not license_id:
            raise WorkerError("config_invalid", f"models-manifest.txt:{number}: пустая license")
        entries.append(ModelFile(subdir, filename, url, sha256, version, license_id))
    if not entries:
        raise WorkerError("config_invalid", "models-manifest.txt: нет ни одной записи")
    keys = [(entry.subdir, entry.filename) for entry in entries]
    if len(keys) != len(set(keys)):
        raise WorkerError("config_invalid", "models-manifest.txt: дубликаты <subdir>/<filename>")
    names = [entry.filename for entry in entries]
    if len(names) != len(set(names)):
        raise WorkerError(
            "config_invalid",
            "models-manifest.txt: одинаковые basenames в разных subdir — graph loader "
            "ссылается по имени, сопоставление было бы неоднозначным",
        )
    return tuple(entries)


class ModelManifest:
    """Упорядоченный pinned-манифест с поиском по имени файла из графа."""

    def __init__(self, entries: tuple[ModelFile, ...]) -> None:
        self._entries = entries
        self._by_name = {entry.filename: entry for entry in entries}

    @classmethod
    def parse(cls, text: str) -> "ModelManifest":
        return cls(parse_manifest(text))

    @classmethod
    def load(cls, path: Path) -> "ModelManifest":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkerError("config_invalid", f"models-manifest.txt не читается: {path}: {exc}") from exc
        return cls.parse(text)

    @property
    def entries(self) -> tuple[ModelFile, ...]:
        return self._entries

    def by_filename(self, filename: str) -> ModelFile | None:
        """Найти pinned-запись по basename из loader-ноды графа."""

        return self._by_name.get(Path(filename).name)


__all__ = ["ModelFile", "ModelManifest", "parse_manifest", "sha256_file"]
