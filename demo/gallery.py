"""traces/index.json: the gallery the Streamlit page lists. Rebuilt from the trace files; recordings still pending are listed as pending."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLE_ORDER = {"analytics": 0, "success_example": 1, "partial_example": 2, "failure_case": 3, "mixed_case": 4, None: 9}


def rebuild_index(traces_dir=ROOT / "traces", questions_file=ROOT / "demo" / "questions.json"):
    d = Path(traces_dir)
    entries = []
    for p in sorted(d.glob("*.json")):
        if p.name == "index.json":
            continue
        t = json.loads(p.read_text(encoding="utf-8"))
        entries.append(dict(trace_id=t["trace_id"], file=p.name, title=t["title"], role=t.get("role"), kind=t["kind"], status=t["status"],
                            question=t["question"], viz=[v["kind"] for v in t["viz"]], model=t["model"]))
    entries.sort(key=lambda e: (ROLE_ORDER.get(e["role"], 9), e["trace_id"]))
    have = {e["trace_id"] for e in entries}
    pending = []
    if Path(questions_file).exists():
        pending = [dict(trace_id=q["id"], title=q["title"], question=q["question"], role=q["role"])
                   for q in json.loads(Path(questions_file).read_text(encoding="utf-8"))["questions"] if q["id"] not in have]
    index = dict(format="trace-gallery-1.0", traces=entries, pending_recordings=pending)
    from .trace import dump_json
    (d / "index.json").write_bytes(dump_json(index))
    return index
