"""End-to-end pipeline smoke test.

Verifies: chroma daemon reachable → context-orchestrator SQLite writable
→ embedding function works → upsert succeeds → search finds the doc →
cleanup removes it.

Spawns through the context-orchestrator venv so we don't have to
duplicate its heavy deps (chromadb, google-genai) in pipeline-monitor.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


def _find_orch_root() -> Path:
    env = os.environ.get("CO_REPO")
    if env and (Path(env) / "src/context_orchestrator").is_dir():
        return Path(env)
    home = Path.home()
    for cand in (
        home / "tasks/vector_databases_experiments",
        home / "tasks/context-orchestrator",
        home / "src/context-orchestrator",
        home / "code/context-orchestrator",
    ):
        if (cand / "src/context_orchestrator").is_dir():
            return cand
    raise FileNotFoundError("could not locate context-orchestrator repo")


_INNER = '''
import sys, json, time, uuid
sys.path.insert(0, "{src_path}")
from context_orchestrator.search import VectorSearch

marker_id = "pipeline-monitor-smoke-" + uuid.uuid4().hex[:8]
marker_text = (
    f"PIPELINE_MONITOR_SMOKE_TEST {{marker_id}} — self-test doc to verify "
    f"end-to-end retrieval. Should be deleted seconds after insertion."
)

t0 = time.time()
result = {{"ok": False, "marker_id": marker_id}}
try:
    vs = VectorSearch()
    vs.add(marker_id, marker_text, {{"type": "smoke_test", "marker": marker_id}})
    hits = vs.search(query=f"PIPELINE_MONITOR_SMOKE_TEST {{marker_id}}",
                     n_results=5, hybrid=True, mmr=False)
    found_rank = next((i for i, h in enumerate(hits) if h.get("id") == marker_id), -1)
    vs.remove(marker_id)
    duration_ms = int((time.time() - t0) * 1000)
    if found_rank < 0:
        result.update({{"stage": "search_recall", "duration_ms": duration_ms,
                       "error": f"marker not in top {{len(hits)}} hits"}})
    else:
        result.update({{"ok": True, "duration_ms": duration_ms,
                       "rank": found_rank + 1, "hits_returned": len(hits),
                       "summary": f"insert→search→delete in {{duration_ms}}ms (rank {{found_rank+1}})"}})
except Exception as e:
    try: vs.remove(marker_id)
    except: pass
    result.update({{"stage": "exception", "error": f"{{type(e).__name__}}: {{e}}"}})
print(json.dumps(result))
'''


def run_smoke_test(timeout_s: int = 30) -> dict[str, Any]:
    """Insert → search → delete a marker doc. Returns result dict."""
    try:
        orch = _find_orch_root()
    except FileNotFoundError as e:
        return {"ok": False, "stage": "locate_orch", "error": str(e)}

    venv_python = orch / ".venv/bin/python"
    if not venv_python.exists():
        return {"ok": False, "stage": "locate_venv",
                "error": f"missing {venv_python} — run setup in {orch}"}

    code = _INNER.format(src_path=str(orch / "src"))
    try:
        r = subprocess.run(
            [str(venv_python), "-c", code],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "subprocess",
                "error": f"smoke test exceeded {timeout_s}s"}
    if r.returncode != 0:
        return {"ok": False, "stage": "subprocess",
                "error": f"exit {r.returncode}: {r.stderr[-300:]}"}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"ok": False, "stage": "parse",
                "error": f"could not parse result: {e}; stdout={r.stdout[:200]}"}


if __name__ == "__main__":
    print(json.dumps(run_smoke_test(), indent=2))
