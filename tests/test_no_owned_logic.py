"""pipeline-monitor orders owners' verbs; it owns no data, recorder or Claude
Code logic (SPEC-v2 §2.3, M3a.3's delete list). These names were in the lab
prototype and must stay deleted; these files are other packages' to touch."""
from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "pipeline_monitor"

DELETED = ("backup_db", "backup_chroma", "verify_chroma_copy", "_chromadb_versions", "hook_command",
           "hook_entry_channel", "hook_entry_path", "edit_hooks", "CLAUDE_MD_BLOCKS", "remove_claude_md_blocks",
           "mcp_set", "skill_link", "_claude_md_template", "mcp_add_cmd")
# Files only their owners write (context-orchestrator: context.db, the chroma
# folder, Claude Code's settings.json / CLAUDE.md / .claude.json).
OWNED_FILES = re.compile(r"""["'/](settings\.json|CLAUDE\.md|\.claude\.json|context\.db|chroma\.sqlite3)["']"""
                         r"""|["']chroma["']\s*\)|/\s*["']chroma["']""")
# The channel/adopt/uninstall layer: none of the above, no sysaudio pinning.
CHECKED = ("owners.py", "channel.py", "modules.py", "mcconfig.py", "adopt.py", "uninstall.py")


def _src(name: str) -> str:
    return (PKG / name).read_text()


def test_deleted_prototype_functions_stay_deleted():
    for path in (PKG / n for n in CHECKED):
        text = path.read_text()
        for name in DELETED:
            assert not re.search(rf"\b{name}\b", text), f"{name} is back in {path.name}"


def test_the_channel_layer_touches_no_owner_files():
    for name in CHECKED:
        text = _src(name)
        assert not OWNED_FILES.search(text), f"{name} touches an owner's file: {OWNED_FILES.search(text)[0]}"
        if name != "mcconfig.py":            # it only READS meeting-capture 0.7's plist (fallback)
            assert "MEETING_CAPTURE_SYSAUDIO" not in text, name
        assert "import chromadb" not in text and "import sqlite3" not in text, name


def test_launchctl_is_never_called_by_the_channel_layer():
    for name in CHECKED:
        assert not re.search(r'\[\s*"launchctl"|"launchctl",', _src(name)), name
