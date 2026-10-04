"""Collectors against synthetic logs / databases (no real daemons)."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

import pytest

from pipeline_monitor import status as st


def _ts(age_s: float = 5) -> str:
    return datetime.fromtimestamp(time.time() - age_s).strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.parametrize("suffix", [".md", ""])     # meeting-capture <= 0.4 / >= 0.5
def test_recording_detected_from_chunk_lines_in_both_log_formats(tmp_path, monkeypatch, suffix):
    log = tmp_path / "daemon.log"
    # The START sentinel is 20 min old (scrolled past the freshness window);
    # only recent chunk lines prove the meeting is still being recorded.
    log.write_text(
        f"{_ts(1200)} INFO mic active — starting recording session\n"
        f"{_ts(1200)} INFO new session: meeting-2026-09-29T10-00-00{suffix}\n"
        f"{_ts(30)} INFO chunk 9.1s [them] -> meeting-2026-09-29T10-00-00{suffix} (226 chars)\n"
    )
    monkeypatch.setattr(st, "MEETING_CAPTURE_LOG", log)
    r = st.log_recording_status()
    assert r["recording"] is True
    assert r["current_file"] == f"meeting-2026-09-29T10-00-00{suffix}"
    assert r["last_chunk_age_s"] < 60 and r["stale"] is False


def test_session_line_names_the_meeting_before_any_chunk(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text(f"{_ts(5)} INFO mic active — starting recording session\n"
                   f"{_ts(4)} INFO new session: meeting-2026-09-29T10-00-00\n")
    monkeypatch.setattr(st, "MEETING_CAPTURE_LOG", log)
    r = st.log_recording_status()
    assert r["recording"] and r["current_file"] == "meeting-2026-09-29T10-00-00"


def test_recent_sessions_come_from_the_database_and_legacy_files(tmp_path, monkeypatch):
    db = tmp_path / "context.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE transcripts (meeting_id TEXT PRIMARY KEY, title TEXT, source TEXT, "
                 "started_at TEXT, body TEXT, content_sha TEXT, created_at REAL, updated_at REAL, indexed_at REAL)")
    now = time.time()
    conn.execute("INSERT INTO transcripts VALUES ('meeting-2026-09-29T10-00-00','meeting-2026-09-29T10-00-00',"
                 "'meeting-capture','', '# Meeting\n[10:00:01] **Me:** hello', '', ?, ?, NULL)", (now - 60, now - 60))
    conn.execute("INSERT INTO transcripts VALUES ('2026-09-28-1400-ep-12','Ep 12 Jane','url','', 'x', '', ?, ?, NULL)",
                 (now - 86400, now - 86400))
    conn.commit(); conn.close()
    legacy = tmp_path / "transcripts"; legacy.mkdir()
    old = legacy / "meeting-2026-01-01T00-00-00.md"
    old.write_text("old")
    import os
    os.utime(old, (now - 30 * 86400, now - 30 * 86400))
    monkeypatch.setattr(st, "CO_DB", db)
    monkeypatch.setattr(st, "TRANSCRIPTS_DIR", legacy)

    r = st.recordings_status()
    assert r["ok"] and r["total_count"] == 3
    names = [s["name"] for s in r["sessions"]]
    assert names[0] == "meeting-2026-09-29T10-00-00"          # newest first
    assert r["sessions"][1]["title"] == "Ep 12 Jane"
    assert r["sessions"][0]["path"] is None and r["sessions"][-1]["path"].endswith(".md")
    assert "hello" in st.transcript_text("meeting-2026-09-29T10-00-00")
    assert st.transcript_text("nope") is None


def test_recent_sessions_without_the_table_still_work(tmp_path, monkeypatch):
    db = tmp_path / "context.db"
    sqlite3.connect(db).execute("CREATE TABLE tasks (id INTEGER)").connection.commit()
    monkeypatch.setattr(st, "CO_DB", db)
    monkeypatch.setattr(st, "TRANSCRIPTS_DIR", tmp_path / "none")
    r = st.recordings_status()
    assert r["ok"] and r["sessions"] == [] and r["total_count"] == 0


# ------------------------------------------------------------ the memory, from context-orchestrator

def _cm(tmp_path, doc=None, rc=0, old=False):
    from conftest import FakeOwner, on_brew
    o = FakeOwner(tmp_path / "cm", "contorch-memory", old=["status"] if old else [])
    if doc is not None:
        o.answer("status", doc, rc=rc)
    on_brew(tmp_path, "contorch-memory", o.path)
    return o


def test_memory_status_is_context_orchestrators_answer(tmp_path):
    _cm(tmp_path, {"schema": "contorch-memory.status/1", "ok": True, "embeddings": "local",
                   "vector_index": "in_process", "docs": 42, "transcripts": 7, "index_compatible": True})
    m = st.memory_status(wait=True)
    assert m["ok"] and m["data"]["docs"] == 42 and m["keyword_only"] is False


def test_memory_status_keyword_only_and_errors(tmp_path):
    o = _cm(tmp_path, {"schema": "contorch-memory.status/1", "ok": True, "vector_index": "none"})
    assert st.memory_status(wait=True)["keyword_only"] is True
    st.ownerstate.clear()
    o.answer("status", {"schema": "contorch-memory.status/1", "ok": False, "vector_index": "in_process",
                        "error": {"code": "chroma_downgrade", "message": "index written by 1.6"}}, rc=1)
    m = st.memory_status(wait=True)
    assert m["ok"] is False and m["error"].startswith("chroma_downgrade")


def test_memory_status_with_an_old_context_orchestrator(tmp_path):
    _cm(tmp_path, old=True)
    m = st.memory_status(wait=True)
    assert m["ok"] is False and "< 0.5" in m["error"]


def test_no_embedding_rules_are_mirrored_here():
    """INV-D2 closed: pm reads `contorch-memory status --json`, it doesn't
    re-derive the embedding choice from the env file and the key."""
    src = (st.Path(st.__file__)).read_text()
    assert "CO_EMBEDDING_MODEL" not in src and "embeddings_status" not in src
    assert "import httpx" not in src and "8765" not in src


# ------------------------------------------------------------ transcription engine

def test_transcription_status_without_the_agent(fake_mc):
    fake_mc.set(rc={"config --json": 2})              # meeting-capture 0.7: the plist says
    t = st.transcription_status(wait=True)
    assert t["ok"] is False and "isn't installed" in t["error"]
    assert fake_mc.reads() == 0                      # stt isn't asked without the recorder


def test_transcription_status_asks_meeting_capture_in_the_background(fake_mc):
    from conftest import write_plist
    write_plist(st.stt.PLIST, {})
    fake_mc.set(sleep=0.3)
    first = st.transcription_status()                # menu bar: never blocks on meeting-capture
    assert first["ok"] and first["status"] == "checking"
    deadline = time.time() + 15
    while time.time() < deadline:
        t = st.transcription_status()
        if t["status"] != "checking":
            break
        time.sleep(0.05)
    assert t["engine"] == "apple" and t["label"] == "on this Mac (en-US)"
    for _ in range(5):                               # 5-second refreshes reuse the answer
        st.transcription_status()
    assert fake_mc.reads() == 1


@pytest.mark.parametrize("t,overall", [
    ({"ok": True, "status": "ok", "attention": False}, "idle"),
    ({"ok": True, "status": "checking", "attention": False}, "idle"),
    ({"ok": True, "status": "error", "attention": False}, "idle"),   # unknown: not flagged
    ({"ok": True, "status": "ok", "attention": True}, "err"),        # nothing can transcribe
    ({"ok": False, "attention": True}, "idle"),
])
def test_overall_flags_when_nothing_can_transcribe(t, overall):
    recorder = {"installed": True, "ok": True, "mode": "batch"}
    assert st.Snapshot(memory={"ok": True}, capture_mode=recorder, transcription=t).overall() == overall
