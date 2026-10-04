"""Sparkle from Python (pipeline_monitor.updates): the PyObjC metadata is
registered before the delegate class exists (else Sparkle's calls segfault),
every delegate method has the Objective-C signature Sparkle calls it with,
the feed choice comes from lifecycle, and the install gate holds while
meeting-capture says recording — or can't tell."""
from __future__ import annotations

import ast
import inspect

import pytest

from pipeline_monitor import lifecycle, owners, updates


# ------------------------------------------------------------ metadata order

def test_metadata_is_registered_before_any_class_is_defined():
    tree = ast.parse(inspect.getsource(updates))
    call_line = next(n.lineno for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                     and getattr(n.value.func, "id", None) == "_register_metadata")
    class_lines = [n.lineno for n in tree.body if isinstance(n, ast.ClassDef)]
    assert class_lines and call_line < min(class_lines)


# The signatures Sparkle 2.10 calls (SPUUpdaterDelegate.h /
# SPUStandardUserDriverDelegate.h): BOOL = Z, NSInteger = q,
# NSError ** = o^@. A block argument is "@" in the signature, with a
# "callable" in its metadata (checked below).
SIGNATURES = {
    "feedURLStringForUpdater_": b"@@:@",
    "updater_mayPerformUpdateCheck_error_": b"Z@:@qo^@",
    "updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_": b"Z@:@@@",
    "updater_willInstallUpdateOnQuit_immediateInstallationBlock_": b"Z@:@@@",
    "updaterShouldRelaunchApplication_": b"Z@:@",
    "updater_didFinishUpdateCycleForUpdateCheck_error_": b"v@:@q@",
    "supportsGentleScheduledUpdateReminders": b"Z@:",
    "standardUserDriverShouldHandleShowingScheduledUpdate_andInImmediateFocus_": b"Z@:@Z",
    "standardUserDriverWillHandleShowingUpdate_forUpdate_state_": b"v@:Z@@",
}


@pytest.mark.parametrize("name,sig", sorted(SIGNATURES.items()))
def test_delegate_signatures(name, sig):
    got = getattr(updates.UpdaterDelegate, name).signature
    assert got == sig, (name, got)


@pytest.mark.parametrize("name", ["updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_",
                                  "updater_willInstallUpdateOnQuit_immediateInstallationBlock_"])
def test_block_arguments_are_callables(name):
    arg = getattr(updates.UpdaterDelegate, name).__metadata__()["arguments"][4]
    assert arg["callable"]["retval"]["type"] == b"v" and len(arg["callable"]["arguments"]) == 1


def test_every_registered_selector_is_implemented():
    for sel in updates.SELECTORS:
        assert hasattr(updates.UpdaterDelegate, sel.decode().replace(":", "_")), sel


# ------------------------------------------------------------ where it runs

def test_not_available_outside_the_app(monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    assert updates.available() is False
    assert updates.why_off() == "not_app"
    u = updates.Updates()
    assert u.start() == {"started": False, "reason": "not_app"}
    assert u.menu_state()["started"] is False


def test_off_when_not_in_applications(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    monkeypatch.setattr(updates, "framework_path", lambda: tmp_path / "Sparkle.framework")
    monkeypatch.setattr(lifecycle, "location", lambda: "translocated")
    assert updates.why_off() == "translocated"


def test_framework_path_needs_this_interpreter_in_the_bundle(monkeypatch):
    # The test interpreter isn't inside an app bundle: no framework, whatever NSBundle says.
    monkeypatch.setattr(owners, "bundle_root", lambda executable=None: None)
    assert updates.framework_path() is None


# ------------------------------------------------------------ feed choice

STABLE = "https://contorch.com/appcast.xml"


def test_feed_stable_is_info_plist():
    assert lifecycle.update_feed() == "stable"
    assert updates.feed_url(STABLE) is None                 # nil: Sparkle uses SUFeedURL


def test_feed_beta_is_the_sibling_appcast():
    lifecycle.set_update_feed("beta")
    assert updates.feed_url(STABLE) == "https://contorch.com/appcast-beta.xml"
    assert updates.feed_url("http://127.0.0.1:18902/appcast.xml") == "http://127.0.0.1:18902/appcast-beta.xml"
    assert updates.feed_url(None) is None


# ------------------------------------------------------------ the install gate

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def gate(answers, prepared=None, prepare_result=True):
    clock = Clock()
    seq = list(answers)
    calls = prepared if prepared is not None else []

    def allowed():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def prepare():
        calls.append("prepare")
        return prepare_result
    return updates.InstallGate(allowed=allowed, prepare=prepare, clock=clock), clock, calls


def test_gate_installs_at_once_when_idle():
    g, _, calls = gate([(True, "idle")])
    block = object()
    g.hold(block)
    assert g.poll() is block
    assert calls == ["prepare"] and not g.waiting


def test_gate_holds_while_recording_then_installs():
    g, _, calls = gate([(False, "recording"), (False, "recording"), (True, "idle")])
    g.hold("go")
    assert g.poll() is None and g.reason == "recording"
    assert g.poll() is None
    assert calls == []                                       # nothing stopped while recording
    assert g.poll() == "go" and calls == ["prepare"]


def test_gate_never_installs_on_unknown_but_offers_the_user_after_10_minutes():
    g, clock, calls = gate([(False, "recording_unknown")])
    g.hold("go")
    for _ in range(3):
        assert g.poll() is None
    assert g.reason == "recording_unknown" and not g.override_offered()
    clock.t += updates.UNKNOWN_OVERRIDE_S - 1
    g.poll()
    assert not g.override_offered()
    clock.t += 2
    g.poll()
    assert g.override_offered() and calls == []
    assert g.install_now() == "go" and calls == ["prepare"]
    assert not g.waiting and not g.override_offered()


def test_gate_unknown_clock_resets_when_the_answer_changes():
    g, clock, _ = gate([(False, "recording_unknown"), (False, "recording"), (False, "recording_unknown")])
    g.hold("go")
    g.poll()
    clock.t += updates.UNKNOWN_OVERRIDE_S + 5
    g.poll()                                                  # recording: a definite answer
    g.poll()                                                  # unknown again: the 10 minutes start over
    assert not g.override_offered()


def test_gate_keeps_holding_when_the_stack_did_not_stop():
    g, _, calls = gate([(True, "idle")], prepare_result=False)
    g.hold("go")
    assert g.poll() is None and g.reason == "prepare_failed" and g.waiting


def test_gate_with_the_real_lifecycle_holds_on_unknown(monkeypatch):
    monkeypatch.setattr(lifecycle, "recording", lambda: None)
    g = updates.InstallGate(prepare=lambda: pytest.fail("must not stop on an unknown"))
    g.hold("go")
    assert g.poll() is None and g.reason == "recording_unknown"
