"""Notifications: UNUserNotificationCenter inside an app bundle, osascript
elsewhere; never raises; the bundle id comes from NSBundle, not a constant."""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from pipeline_monitor import notify


@pytest.fixture
def osa(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(notify.subprocess, "run", run)
    return calls


def test_outside_a_bundle_it_uses_osascript(osa, monkeypatch):
    monkeypatch.setattr(notify, "bundle_identifier", lambda: None)
    assert notify.post("contorch", "Say \"hi\"", "a\\b") == "osascript"
    assert osa[0][:2] == ["osascript", "-e"]
    assert 'subtitle "Say \\"hi\\""' in osa[0][2] and '"a\\\\b"' in osa[0][2]
    assert notify.request_authorization() is False


def test_inside_a_bundle_it_uses_the_notification_center(osa, monkeypatch):
    posted = []

    class Content:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            self.fields = {}
            return self

        def __getattr__(self, name):
            return lambda v: self.fields.__setitem__(name, v)

    class Center:
        def addNotificationRequest_withCompletionHandler_(self, req, handler):
            posted.append(req)

        def requestAuthorizationWithOptions_completionHandler_(self, opts, handler):
            posted.append(("auth", opts))

    UN = types.SimpleNamespace(
        UNUserNotificationCenter=types.SimpleNamespace(currentNotificationCenter=lambda: Center()),
        UNMutableNotificationContent=Content,
        UNNotificationRequest=types.SimpleNamespace(
            requestWithIdentifier_content_trigger_=lambda i, c, t: (i, c.fields)),
        UNAuthorizationOptionAlert=4, UNAuthorizationOptionSound=2)
    monkeypatch.setitem(sys.modules, "UserNotifications", UN)
    monkeypatch.setattr(notify, "bundle_identifier", lambda: "com.contorch.labtest.app")
    assert notify.post("contorch", "Copied", "body") == "un"
    ident, fields = posted[0]
    assert ident.startswith("contorch-") and fields == {
        "setTitle_": "contorch", "setSubtitle_": "Copied", "setBody_": "body"}
    assert osa == []
    assert notify.request_authorization() is True and posted[-1] == ("auth", 6)


def test_a_broken_center_falls_back_and_never_raises(osa, monkeypatch):
    monkeypatch.setattr(notify, "bundle_identifier", lambda: "com.contorch.labtest.app")
    monkeypatch.setitem(sys.modules, "UserNotifications", None)       # import fails
    assert notify.post("contorch", "t", "b") == "osascript"


def test_bundle_identifier_is_none_for_a_bare_python():
    # pytest runs from a venv's python, not an .app bundle
    assert notify.bundle_identifier() is None
