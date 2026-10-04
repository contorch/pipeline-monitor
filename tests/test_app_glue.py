"""The Contorch.app part of the menu (pipeline_monitor.app): which items
exist in which channel, what Report a problem… sends (versions, no logs), and
the reopen handler's hookup on rumps' application delegate."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from pipeline_monitor import app, owners


def test_report_url_has_versions_and_no_logs(monkeypatch):
    monkeypatch.setattr(app, "app_version", lambda: "0.4.0 (12)")
    u = urlparse(app.report_url())
    assert u.netloc == "github.com" and u.path == "/contorch/contorch-macos/issues/new"
    body = parse_qs(u.query)["body"][0]
    assert "Contorch.app: 0.4.0 (12)" in body and "macOS" in body and "pipeline-monitor:" in body
    assert "daemon.log" not in body and "menubar.log" not in body


def test_in_app_needs_channel_and_bundle(monkeypatch, tmp_path):
    assert app.in_app() is False
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    assert app.in_app() is False                                   # the test interpreter isn't in a bundle
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: tmp_path / "Contorch.app")
    assert app.in_app() is True


def test_has_verb():
    assert app.has_verb("lifecycle") is True
    assert app.has_verb("no_such_verb") is False


def test_setup_row_opens_setup_in_the_app_and_copies_elsewhere(monkeypatch):
    class Snap:
        modules = {"modules": [{"id": "x"}]}
    monkeypatch.setattr(app.modules, "menu_lines", lambda m: [{"id": "setup"}])
    called = []
    rows = app._module_rows(Snap(), on_setup=lambda _: called.append(1))
    assert [r.title for r in rows] == ["Set up Contorch…"]
    rows[0].callback(None)
    assert called == [1]
    rows = app._module_rows(Snap())                                # brew/dev: the copy-the-command callback
    assert rows[0].callback is not None


def test_reopen_handler_is_added_to_rumps_delegate():
    sel = b"applicationShouldHandleReopen:hasVisibleWindows:"

    class FakeApp:
        def __init__(self):
            self.n = 0

        def on_reopen(self):
            self.n += 1
    app.install_reopen(FakeApp())
    assert app.rumps.rumps.NSApp.instancesRespondToSelector_(sel)
    app.install_reopen(FakeApp())                                  # idempotent
    m = app.rumps.rumps.NSApp.instanceMethodSignatureForSelector_(sel)
    assert m.methodReturnType() in (b"Z", b"B") and m.numberOfArguments() == 4
