"""The menu bar icons ship in the wheel (package data). They used to live in
the repo root, which no install (brew's venv, the app bundle) contains, so the
menu bar showed a text "○" instead of the Contorch glyph."""
from __future__ import annotations

import subprocess
import sys
import zipfile
from importlib import resources
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GLYPHS = ("glyph-template.png", "glyph-template-pulse.png", "glyph-rec.png")


def test_glyphs_are_package_data():
    base = resources.files("pipeline_monitor") / "assets"
    for name in GLYPHS:
        data = (base / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name


def test_the_wheel_contains_the_glyphs(tmp_path):
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(tmp_path), str(ROOT)],
                   check=True, capture_output=True, text=True, timeout=600)
    wheel = next(tmp_path.glob("pipeline_monitor-*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    for name in GLYPHS:
        assert f"pipeline_monitor/assets/{name}" in names
    # the build script and the README's logo stay out of the package
    assert not any(n.endswith("build-menubar-icons.py") for n in names)


def test_app_loads_icons_from_the_package_not_the_repo_root():
    src = (ROOT / "pipeline_monitor" / "app.py").read_text()
    assert 'resources.files(__package__) / "assets"' in src
    assert "parent.parent / \"assets\"" not in src
