"""User notifications for the menu bar and `contorch` (Contorch.app or not).

Inside Contorch.app (a bundle with an identifier) they go through
UNUserNotificationCenter, so they carry the app's name and icon and follow its
row in System Settings › Notifications. Elsewhere (Homebrew, a source
checkout: a bare Python with no bundle) UNUserNotificationCenter refuses to
work, so they go through `osascript -e 'display notification …'`, which needs
no permission. rumps.notification is neither: it silently does nothing without
a signed bundle.

The permission is asked once, from `contorch setup` (request_authorization());
posting never asks. Every function here is best effort and never raises:
notifications are optional UX.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid


def bundle_identifier() -> str | None:
    """The running app's bundle id (com.contorch.app, or a labtest id), or
    None outside an app bundle. Read from NSBundle, never a constant. A
    framework Python (Homebrew's) also has a main bundle, its Python.app, but
    this interpreter isn't inside it: only a bundle that contains
    sys.executable counts."""
    try:
        from Foundation import NSBundle
        b = NSBundle.mainBundle()
        ident = b.bundleIdentifier()
        path = str(b.bundlePath() or "")
    except Exception:
        return None
    if not ident or not path.endswith(".app"):
        return None
    contents = os.path.join(os.path.realpath(path), "Contents") + os.sep
    return str(ident) if os.path.realpath(sys.executable).startswith(contents) else None


def _center():
    """UNUserNotificationCenter for this bundle, or None (not bundled, or the
    UserNotifications bindings — the `app` extra — aren't installed)."""
    if not bundle_identifier():
        return None
    try:
        import UserNotifications as UN
        return UN.UNUserNotificationCenter.currentNotificationCenter()
    except Exception:
        return None


def _osascript(app: str, title: str, body: str) -> bool:
    def esc(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(body)}" with title "{esc(app)}" subtitle "{esc(title)}"'
    try:
        return subprocess.run(["osascript", "-e", script], timeout=3, check=False,
                              capture_output=True).returncode == 0
    except Exception:
        return False


def post(app: str, title: str, body: str) -> str:
    """Show a notification; returns the route used ("un" | "osascript" | "none")."""
    center = _center()
    if center is not None:
        try:
            import UserNotifications as UN
            content = UN.UNMutableNotificationContent.alloc().init()
            content.setTitle_(app)
            content.setSubtitle_(title)
            content.setBody_(body)
            req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                f"contorch-{uuid.uuid4().hex}", content, None)
            center.addNotificationRequest_withCompletionHandler_(req, None)
            return "un"
        except Exception:
            pass
    return "osascript" if _osascript(app, title, body) else "none"


def request_authorization() -> bool:
    """Ask macOS once for permission to notify (Contorch.app only; a no-op
    elsewhere, where osascript needs none). Returns whether it was asked."""
    center = _center()
    if center is None:
        return False
    try:
        import UserNotifications as UN
        opts = UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound
        center.requestAuthorizationWithOptions_completionHandler_(opts, lambda granted, err: None)
        return True
    except Exception:
        return False
