"""Set up Contorch… (pipeline_monitor.setup_launcher): the .command the app
writes at runtime — exact content, mode 0755, no quarantine attribute — and
how it is opened."""
from __future__ import annotations

import ctypes
import os
import stat
import subprocess

import pytest

from pipeline_monitor import owners, setup_launcher


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "Contorch.app"
    (app / "Contents" / "Resources" / "bin").mkdir(parents=True)
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: app)
    return app


def test_path():
    assert str(setup_launcher.command_path()).endswith(
        "/Library/Application Support/Contorch/Set Up Contorch.command")


def test_content_mode_and_no_quarantine(bundle):
    p = setup_launcher.write()
    assert p == setup_launcher.command_path()
    exe = bundle / "Contents" / "Resources" / "bin" / "contorch"
    assert p.read_text() == f'#!/bin/sh\nexec "{exe}" setup\n'
    assert stat.S_IMODE(p.stat().st_mode) == 0o755
    assert not setup_launcher.has_quarantine(p)
    assert "com.apple.quarantine" not in subprocess.run(["xattr", str(p)], capture_output=True, text=True).stdout


def test_quoting_survives_odd_paths(tmp_path, monkeypatch):
    app = tmp_path / 'My "Apps" $HOME `x`' / "Contorch.app"
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: app)
    exe = app / "Contents" / "Resources" / "bin" / "contorch"
    exe.parent.mkdir(parents=True)
    exe.write_text('#!/bin/sh\necho "ran $0 with $*"\n')
    exe.chmod(0o755)
    p = setup_launcher.write()
    out = subprocess.run(["/bin/sh", str(p)], capture_output=True, text=True).stdout.strip()
    assert out == f"ran {exe} with setup"


def test_an_existing_quarantine_attribute_is_removed(bundle):
    p = setup_launcher.command_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("old")
    libc = ctypes.CDLL(None)
    val = b"0081;00000000;Safari;"
    assert libc.setxattr(os.fsencode(str(p)), b"com.apple.quarantine", val, len(val), 0, 0) == 0
    assert setup_launcher.has_quarantine(p)
    # write() replaces the file (a new inode has no attribute) and checks again
    setup_launcher.write()
    assert not setup_launcher.has_quarantine(p)


def test_rewritten_for_a_moved_app(bundle, tmp_path, monkeypatch):
    setup_launcher.write()
    moved = tmp_path / "elsewhere" / "Contorch.app"
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: moved)
    p = setup_launcher.write()
    assert str(moved) in p.read_text() and str(bundle) not in p.read_text()


def test_outside_the_app_it_refuses(monkeypatch):
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: None)
    res = setup_launcher.launch()
    assert res["ok"] is False and "contorch setup" in res["error"]


def test_launch_opens_terminal(bundle, monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(setup_launcher.subprocess, "run", fake_run)
    res = setup_launcher.launch()
    assert res["ok"] and seen == [["open", "-a", "Terminal", str(setup_launcher.command_path())]]
