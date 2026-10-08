"""Recompute the `trust` section of the stored traces with the CURRENT automated checks. No model is called and nothing is re-recorded.

  python -m demo.retrust            # rewrite `trust` where it changed, then validate every trace
  python -m demo.retrust --check    # only report; exit status 1 if a stored `trust` differs from the current checks

Only the `trust` field can change (answer, SQL rows, plan and evaluation are never touched). Use it after the checks themselves were corrected.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rag.corpus import load_corpus  # noqa: E402

from .trace import dump_json, trust_checks, validate_trace  # noqa: E402

TRACES = ROOT / "traces"


def record_of(trace):
    r = trace["retrieval"]
    return dict(question=trace["question"], answer=trace["answer"], plan=trace["plan"], tasks=trace["tasks"], status=trace["status"],
                query_status=r["status"], context_ids=r["context_ids"])


def retrust(traces_dir=TRACES, check_only=False):
    """Returns the list of trace ids whose stored `trust` differed from the current checks."""
    docs = {d.doc_id: d for d in load_corpus()}
    text = {k: d.text for k, d in docs.items()}
    changed = []
    for p in sorted(Path(traces_dir).glob("*.json")):
        if p.name == "index.json":
            continue
        t = json.loads(p.read_text(encoding="utf-8"))
        new = trust_checks(record_of(t), text)
        if new != t["trust"]:
            changed.append(t["trace_id"])
            if not check_only:
                t["trust"] = new
                p.write_bytes(dump_json(t))
        if not check_only:
            problems = validate_trace(json.loads(p.read_text(encoding="utf-8")), docs)
            if problems:
                raise SystemExit(f"{t['trace_id']}: invalid trace after retrust: {problems}")
    return changed


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    ch = retrust(check_only=a.check)
    print(("would change" if a.check else "updated") + " trust in:", ch or "no trace (already current)")
    sys.exit(1 if (a.check and ch) else 0)
