"""Sparkle 2 updates for Contorch.app, driven from Python (SPEC-v2 §5.3).

Only inside Contorch.app: Sparkle.framework is loaded from the running app's
Contents/Frameworks, so a Homebrew or source install never gets here
(available() is False and the menu has no update items; brew upgrades with
`brew upgrade`).

Every decision is Python's, in lifecycle.py; this module only wires Sparkle's
delegate calls to it:

    may an update install now?   lifecycle.install_allowed(): only when
                                 `meeting-capture status --json` says
                                 recording: false. Recording, or can't tell,
                                 HOLDS the install (Sparkle's
                                 shouldPostponeRelaunchForUpdate), re-asked
                                 every POLL_S seconds.
    before the bundle swap       lifecycle.prepare_update(): the stack stops
                                 (reason=update); v2's on_launch resumes it.
    which feed                   lifecycle.update_feed(): "beta" answers
                                 feedURLStringForUpdater: with the beta appcast
                                 next to Info.plist's SUFeedURL
                                 (appcast-beta.xml); "stable" answers nil, so
                                 SUFeedURL is used.
    install on quit              willInstallUpdateOnQuit marks the update
                                 staged (lifecycle.update_staged), so Quit
                                 stops the recorder whatever "Keep recording
                                 after Quit" says.

A hold that can't tell whether a meeting is being recorded for longer than
UNKNOWN_OVERRIDE_S offers the user "Install now (stops recording)" — their
explicit choice; Python never installs on an unknown by itself.

Info.plist sets SUAutomaticallyUpdate NO: no silent download, so nothing can
install behind the user's back on quit. Sparkle's own settings (automatic
checks) live in Sparkle's user defaults and override Info.plist; anything that
shows or changes them goes through SPUUpdater, never the plist.

PyObjC: the delegate's selector metadata (BOOL returns, block and NSError**
arguments) MUST be registered before the delegate class is defined — PyObjC
fixes a Python method's Objective-C signature when the class is created, and
with the default all-object signature Sparkle's call into
updater:mayPerformUpdateCheck:error: segfaulted (PROVEN in the lab).
tests/test_updates_metadata.py checks the order and every signature.

Sparkle must run as its own responsible process (from a terminal's child it
asked for an admin password, M0): the menu bar is launched by LaunchServices
or the login item, so it is.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import objc
from Foundation import NSBundle, NSObject

from . import lifecycle, owners

POLL_S = 5.0
UNKNOWN_OVERRIDE_S = 600.0
BETA_APPCAST = "appcast-beta.xml"

_V = {"type": b"v"}
_Z = {"type": b"Z"}
_BLOCK = {"callable": {"retval": _V, "arguments": {0: {"type": b"^v"}}}}

# selector -> PyObjC metadata. The ONE table: registered below, before the
# classes, and checked by tests/test_updates_metadata.py.
SELECTORS: dict[bytes, dict] = {
    # SPUUpdaterDelegate
    b"feedURLStringForUpdater:": {"retval": {"type": b"@"}},
    b"updater:mayPerformUpdateCheck:error:": {
        "retval": _Z, "arguments": {3: {"type": b"q"}, 4: {"type": b"^@", "type_modifier": b"o"}}},
    b"updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:": {"retval": _Z, "arguments": {4: _BLOCK}},
    b"updater:willInstallUpdateOnQuit:immediateInstallationBlock:": {"retval": _Z, "arguments": {4: _BLOCK}},
    b"updaterShouldRelaunchApplication:": {"retval": _Z},
    b"updater:didFinishUpdateCycleForUpdateCheck:error:": {"arguments": {3: {"type": b"q"}}},
    # SPUStandardUserDriverDelegate (gentle reminders for a menu bar app)
    b"supportsGentleScheduledUpdateReminders": {"retval": _Z},
    b"standardUserDriverShouldHandleShowingScheduledUpdate:andInImmediateFocus:": {
        "retval": _Z, "arguments": {3: _Z}},
    b"standardUserDriverWillHandleShowingUpdate:forUpdate:state:": {"arguments": {2: _Z}},
}


def _register_metadata() -> None:
    for sel, meta in SELECTORS.items():
        objc.registerMetaDataForSelector(b"NSObject", sel, meta)


# BEFORE any class below: see the module docstring.
_register_metadata()


# ------------------------------------------------------------------ facts

def framework_path() -> Path | None:
    """Contents/Frameworks/Sparkle.framework of the running app, or None.
    Only the menu bar process sees the app as its main bundle (inside
    contorch-python the stub's embedded Info.plist answers), and the bundle
    must contain this interpreter (a framework Python's Python.app doesn't)."""
    try:
        b = NSBundle.mainBundle()
        fw = b.privateFrameworksPath()
        path = str(b.bundlePath() or "")
    except Exception:
        return None
    root = owners.bundle_root()
    if not fw or root is None or Path(path).resolve() != root.resolve():
        return None
    p = Path(str(fw)) / "Sparkle.framework"
    return p if p.is_dir() else None


def available() -> bool:
    return owners.channel() == "app" and framework_path() is not None


def why_off() -> str | None:
    """None when updates may run here; otherwise why not (the menu says so)."""
    if owners.channel() != "app":
        return "not_app"
    if framework_path() is None:
        return "no_framework"
    loc = lifecycle.location()
    if loc != "ok":
        return loc                       # translocated | read_only | outside_applications
    return None


def feed_url(stable: str | None, feed: str | None = None) -> str | None:
    """What feedURLStringForUpdater: answers: None (= Info.plist's SUFeedURL)
    for the stable feed; the beta appcast beside it for the beta feed."""
    feed = feed or lifecycle.update_feed()
    if feed != "beta" or not stable or "/" not in stable:
        return None
    return stable.rsplit("/", 1)[0] + "/" + BETA_APPCAST


# ------------------------------------------------------------------ the install gate (pure Python)

class InstallGate:
    """Holds a staged install until lifecycle says it may go.

    hold(release) keeps Sparkle's "go" block; poll() asks install_allowed()
    and, when allowed, runs prepare_update() and returns True (the caller
    then invokes the block on the main thread). While the answer is
    recording_unknown for UNKNOWN_OVERRIDE_S, override_offered() is True and
    install_now() — the user's explicit choice — prepares and releases."""

    def __init__(self, allowed: Callable[[], tuple[bool, str]] = None, prepare: Callable[[], Any] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._allowed = allowed or lifecycle.install_allowed
        self._prepare = prepare or lifecycle.prepare_update
        self._clock = clock
        self._lock = threading.Lock()
        self.release: Callable[[], None] | None = None
        self.reason: str | None = None
        self.unknown_since: float | None = None
        self.held_at: float | None = None
        self.polls = 0

    @property
    def waiting(self) -> bool:
        return self.release is not None

    def hold(self, release: Callable[[], None]) -> None:
        with self._lock:
            self.release = release
            self.held_at = self._clock()
            self.reason = None
            self.unknown_since = None
            self.polls = 0

    def _take(self) -> Callable[[], None] | None:
        with self._lock:
            r, self.release = self.release, None
            return r

    def poll(self) -> Callable[[], None] | None:
        """One check. Returns the block to invoke when the install may go
        (after prepare_update()), else None."""
        if not self.waiting:
            return None
        ok, why = self._allowed()
        self.polls += 1
        if not ok:
            if why == "recording_unknown":
                if self.unknown_since is None:
                    self.unknown_since = self._clock()
            else:
                self.unknown_since = None
            self.reason = why
            return None
        if self._prepare() is False:     # the stack didn't stop: keep holding, ask again
            self.reason = "prepare_failed"
            return None
        self.reason = "idle"
        return self._take()

    def override_offered(self) -> bool:
        return (self.waiting and self.reason == "recording_unknown" and self.unknown_since is not None
                and self._clock() - self.unknown_since >= UNKNOWN_OVERRIDE_S)

    def install_now(self) -> Callable[[], None] | None:
        """The user chose "Install now (stops recording)"."""
        if not self.waiting:
            return None
        self._prepare()
        return self._take()

    def state(self) -> dict:
        return {"waiting": self.waiting, "reason": self.reason, "polls": self.polls,
                "override_offered": self.override_offered()}


# ------------------------------------------------------------------ Sparkle glue

def _call_main(fn: Callable, *args) -> None:
    from PyObjCTools import AppHelper
    AppHelper.callAfter(fn, *args)


def _version(item) -> str | None:
    try:
        return str(item.displayVersionString())
    except Exception:
        return None


class UpdaterDelegate(NSObject):
    """SPUUpdaterDelegate + SPUStandardUserDriverDelegate. Every answer
    comes from lifecycle (via the Updates object); nothing is decided here."""

    def initWithUpdates_(self, updates):
        self = objc.super(UpdaterDelegate, self).init()
        if self is None:
            return None
        self.updates = updates
        return self

    # ---- which feed
    def feedURLStringForUpdater_(self, updater):
        return self.updates.feed_url()

    # ---- checks are always allowed once the updater runs (it only starts from /Applications)
    def updater_mayPerformUpdateCheck_error_(self, updater, check, error):
        return True, None

    # ---- the install gate
    def updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(self, updater, item, block):
        self.updates.hold(block, _version(item))
        return True                      # always: Python stops the recorder first

    def updater_willInstallUpdateOnQuit_immediateInstallationBlock_(self, updater, item, block):
        lifecycle.update_staged = True   # Quit stops the recorder (reason=update)
        self.updates.log(f"update {_version(item)} will install on quit")
        return False

    def updaterShouldRelaunchApplication_(self, updater):
        return True

    def updaterWillRelaunchApplication_(self, updater):
        lifecycle.update_staged = True

    def updater_didAbortWithError_(self, updater, error):
        self.updates.log(f"update aborted: {error.localizedDescription()}")

    def updater_didFinishUpdateCycleForUpdateCheck_error_(self, updater, check, error):
        self.updates.changed()

    # ---- gentle reminders: a menu bar app shows "Update Available…" instead of a window
    def supportsGentleScheduledUpdateReminders(self):
        return True

    def standardUserDriverShouldHandleShowingScheduledUpdate_andInImmediateFocus_(self, item, immediate):
        return bool(immediate)

    def standardUserDriverWillHandleShowingUpdate_forUpdate_state_(self, handle, item, state):
        if not handle:
            self.updates.remind(_version(item))

    def standardUserDriverDidReceiveUserAttentionForUpdate_(self, item):
        self.updates.remind(None)

    def standardUserDriverWillFinishUpdateSession(self):
        self.updates.remind(None)


class Updates:
    """The app's one updater. start() loads Sparkle and starts
    SPUStandardUpdaterController when why_off() is None."""

    def __init__(self, on_change: Callable[[], None] | None = None, log: Callable[[str], None] | None = None):
        self.controller = None
        self.delegate = None
        self.gate = InstallGate()
        self.pending_version: str | None = None      # a gentle reminder to show
        self.installing_version: str | None = None
        self.off_reason: str | None = "not_started"
        self._on_change = on_change or (lambda: None)
        self._log = log or (lambda m: None)
        self._thread: threading.Thread | None = None

    # -- lifecycle
    def start(self) -> dict:
        why = why_off()
        if why:
            self.off_reason = why
            return {"started": False, "reason": why}
        try:
            objc.loadBundle("Sparkle", {}, bundle_path=str(framework_path()))
            ctl_cls = objc.lookUpClass("SPUStandardUpdaterController")
            self.delegate = UpdaterDelegate.alloc().initWithUpdates_(self)
            self.controller = ctl_cls.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
                True, self.delegate, self.delegate)
        except Exception as e:  # noqa: BLE001 — never keep the menu bar from running
            self.off_reason = "error"
            self._log(f"Sparkle didn't start: {e!r}")
            return {"started": False, "reason": "error", "error": repr(e)}
        self.off_reason = None
        return {"started": True}

    @property
    def started(self) -> bool:
        return self.controller is not None

    def feed_url(self) -> str | None:
        try:
            stable = NSBundle.mainBundle().objectForInfoDictionaryKey_("SUFeedURL")
        except Exception:
            stable = None
        return feed_url(str(stable) if stable else None)

    # -- user actions
    def check(self) -> None:
        """Check for Updates… (Sparkle's standard window)."""
        if self.controller is not None:
            self.pending_version = None
            self.controller.checkForUpdates_(None)

    def can_check(self) -> bool:
        try:
            return bool(self.controller and self.controller.updater().canCheckForUpdates())
        except Exception:
            return False

    def install_now(self) -> None:
        """Install now (stops recording): the user's override of a hold that
        can't tell whether a meeting is being recorded."""
        def _go():
            block = self.gate.install_now()
            if block is not None:
                _call_main(block)
        threading.Thread(target=_go, name="update-install-now", daemon=True).start()

    # -- delegate callbacks
    def hold(self, block, version: str | None) -> None:
        self.installing_version = version
        self.gate.hold(block)
        self.changed()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._wait, name="update-gate", daemon=True)
            self._thread.start()

    def _wait(self) -> None:
        while self.gate.waiting:
            try:
                block = self.gate.poll()
            except Exception as e:  # noqa: BLE001 — a failing check holds the install
                self._log(f"update gate check failed: {e!r}")
                block = None
            if block is not None:
                self._log(f"installing update {self.installing_version}: nothing is being recorded")
                _call_main(block)
                return
            self.changed()
            time.sleep(POLL_S)

    def remind(self, version: str | None) -> None:
        self.pending_version = version
        self.changed()

    def changed(self) -> None:
        try:
            _call_main(self._on_change)
        except Exception:
            pass

    def log(self, msg: str) -> None:
        self._log(msg)

    # -- what the menu shows
    def menu_state(self) -> dict:
        g = self.gate.state()
        return {"started": self.started, "off_reason": self.off_reason, "pending_version": self.pending_version,
                "installing_version": self.installing_version, **g}
