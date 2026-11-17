"""models-manifest.txt: только commit-pinned URL, sha256, version/license, layout."""

from __future__ import annotations

import re
from pathlib import Path

from audio_worker.manifest import ModelManifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "models-manifest.txt"

PIN_RE = re.compile(r"/resolve/([0-9a-f]{40})/")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _rows() -> list[list[str]]:
    rows = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rows.append(line.split("\t"))
    return rows


def test_manifest_parses() -> None:
    manifest = ModelManifest.load(MANIFEST)
    assert {entry.filename for entry in manifest.entries} >= {
        "stable_audio_3_small_sfx.safetensors",
        "t5gemma_b_b_ul2.safetensors",
        "ace_step_1.5_turbo_aio.safetensors",
    }


def test_all_rows_have_six_columns() -> None:
    rows = _rows()
    assert rows, "манифест пуст"
    for row in rows:
        assert len(row) == 6, f"ожидалось <subdir>\\t<filename>\\t<URL>\\t<sha256>\\t<version>\\t<license>: {row!r}"


def test_urls_are_commit_pinned_and_named() -> None:
    for subdir, name, url, sha, version, license_id in _rows():
        assert "/resolve/main/" not in url and "/resolve/master/" not in url, (subdir, name, url)
        assert PIN_RE.search(url) is not None, f"{name}: URL не запинен на commit: {url}"
        assert name in url, f"{name}: имя файла не совпадает с URL {url}"
        assert SHA_RE.fullmatch(sha), f"{name}: sha256 не 64 hex"
        assert "@" in version, f"{name}: version без @: {version!r}"
        assert license_id, f"{name}: пустая license"


def test_no_duplicate_names() -> None:
    rows = _rows()
    keys = [(row[0], row[1]) for row in rows]
    assert len(keys) == len(set(keys))


def test_expected_layout_and_licenses() -> None:
    by_name = {row[1]: row for row in _rows()}
    assert by_name["stable_audio_3_small_sfx.safetensors"][0] == "checkpoints"
    assert by_name["t5gemma_b_b_ul2.safetensors"][0] == "text_encoders"
    assert by_name["ace_step_1.5_turbo_aio.safetensors"][0] == "checkpoints"
    assert by_name["stable_audio_3_small_sfx.safetensors"][5] == "Stability-AI-Community"
    assert by_name["ace_step_1.5_turbo_aio.safetensors"][5] == "MIT"


def test_weights_are_not_in_git() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "models/" in gitignore
    assert not list(ROOT.glob("**/*.safetensors")), "веса не должны лежать в репозитории"


def test_populate_volume_script_exists() -> None:
    script = ROOT / "populate-volume.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "MODELS_MANIFEST" in text and "sha256" in text
