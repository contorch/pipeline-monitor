"""Setup orders the owners' verbs; Claude Code, the search index and the
recorder's settings are theirs (SPEC-v2 M3b.1, RL-1/RL-2). The copies pm used
to have stay deleted."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "pipeline_monitor" / "contorch.py").read_text()


def test_pms_claude_code_integration_is_gone():
    for name in ("mcp_add_cmd", "_claude_md_template", "_existing_mcp_env", "_existing_mcp_entry", "CLAUDE_MD",
                 "CLAUDE_JSON", "claude-md-template"):
        assert name not in SRC, name
    assert not re.search(r'\[claude, "mcp", "(add|remove|add-json)"', SRC)
    assert "contorch-memory\", \"claude\", \"install\"" in SRC


def test_setup_never_copies_or_decides_the_search_index():
    for name in ("copytree", "CHROMA_DIR", "chroma.backup-before-contorch-setup", "context-orchestrator-chroma\"], \"install\""):
        assert name not in SRC, name
    assert '"index", "migrate", "--in-process"' in SRC


def test_no_owner_files_are_touched_by_setup():
    for f in ("settings.json", "CLAUDE.md", ".claude.json", "context.db"):
        assert f'"{f}"' not in SRC and f"/ \"{f}\"" not in SRC, f
