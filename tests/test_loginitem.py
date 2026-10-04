"""Open at Login (pipeline_monitor.loginitem) with SMAppService stubbed:
status mapping and cache, register only from Applications, off is off."""
from __future__ import annotations

import pytest

from pipeline_monitor import lifecycle, loginitem, owners


class FakeService:
    def __init__(self, status=0):
        self._status = status
        self.calls = []
        self.fail = None

    def status(self):
        self.calls.append("status")
        return self._status

    def registerAndReturnError_(self, _):
        self.calls.append("register")
        if self.fail:
            return False, self.fail
        self._status = 1
        return True, None

    def unregisterAndReturnError_(self, _):
        self.calls.append("unregister")
        if self._status == 0:
            return False, FakeError(1, "not registered")
        self._status = 0
        return True, None


class FakeError:
    def __init__(self, code, msg):
        self._c, self._m = code, msg

    def domain(self):
        return "SMAppServiceErrorDomain"

    def code(self):
        return self._c

    def localizedDescription(self):
        return self._m


class FakeSM:
    def __init__(self, svc):
        self.svc = svc
        self.opened = 0
        outer = self

        class SMAppService:
            @staticmethod
            def mainAppService():
                return outer.svc

            @staticmethod
            def openSystemSettingsLoginItems():
                outer.opened += 1
        self.SMAppService = SMAppService


@pytest.fixture
def svc(monkeypatch, tmp_path):
    s = FakeService()
    sm = FakeSM(s)
    monkeypatch.setattr(loginitem, "_sm", lambda: sm)
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: tmp_path / "Applications" / "Contorch.app")
    monkeypatch.setattr(lifecycle, "location", lambda: "ok")
    loginitem.clear_cache()
    s.sm = sm
    return s


def test_unavailable_outside_the_app(monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    assert loginitem.available() is False
    assert loginitem.status() == "unavailable"
    assert loginitem.register()["error"]["code"] == "not_app"


@pytest.mark.parametrize("raw,name,on", [(0, "not_registered", False), (1, "enabled", True),
                                         (2, "requires_approval", False), (3, "not_found", False)])
def test_status_mapping(svc, raw, name, on):
    svc._status = raw
    assert loginitem.status() == name
    assert loginitem.is_on(force=True) is on


def test_status_is_cached_for_ten_seconds(svc, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(loginitem.time, "monotonic", lambda: t[0])
    loginitem.status()
    loginitem.status()
    assert svc.calls.count("status") == 1
    t[0] += loginitem.CACHE_S + 0.1
    loginitem.status()
    assert svc.calls.count("status") == 2
    loginitem.status(force=True)
    assert svc.calls.count("status") == 3


def test_register_and_unregister(svc):
    res = loginitem.register()
    assert res == {"ok": True, "status": "enabled", "error": None}
    assert loginitem.is_on()
    res = loginitem.unregister()
    assert res["ok"] and res["status"] == "not_registered"


def test_unregister_when_already_off_is_ok(svc):
    assert loginitem.unregister()["ok"] is True


@pytest.mark.parametrize("loc", ["translocated", "read_only", "outside_applications"])
def test_register_refused_outside_applications(svc, monkeypatch, loc):
    monkeypatch.setattr(lifecycle, "location", lambda: loc)
    res = loginitem.register()
    assert not res["ok"] and res["error"] == {"code": "location", "location": loc,
                                              "message": "Move Contorch to Applications first"}
    assert "register" not in svc.calls


def test_register_error_is_reported(svc):
    svc.fail = FakeError(22, "Operation not permitted")
    res = loginitem.register()
    assert not res["ok"] and res["error"]["number"] == 22 and res["status"] == "not_registered"


def test_open_settings(svc):
    assert loginitem.open_settings() and svc.sm.opened == 1


# ------------------------------------------------------------ setup's menu bar step in the app

def test_setup_turns_on_open_at_login_in_the_app(svc, monkeypatch):
    from pipeline_monitor import contorch, notify
    monkeypatch.setattr(notify, "request_authorization", lambda: False)
    lines, todo = [], []
    contorch._menu_bar(lines.append, todo, "app")
    assert svc.calls.count("register") == 1 and todo == []
    assert any("opens at login" in line for line in lines)


def test_setup_leaves_a_todo_when_it_cannot(svc, monkeypatch):
    from pipeline_monitor import contorch, notify
    monkeypatch.setattr(notify, "request_authorization", lambda: False)
    monkeypatch.setattr(lifecycle, "location", lambda: "translocated")
    lines, todo = [], []
    contorch._menu_bar(lines.append, todo, "app")
    assert "register" not in svc.calls and todo and "Move Contorch to Applications first" in todo[0]
