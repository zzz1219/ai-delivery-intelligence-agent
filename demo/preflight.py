"""Pre-flight check for the machine that runs the demo. Run from the delivery_agent folder:

    python -m demo.preflight

Reports what is installed, whether the frozen specifications and the shipped traces are intact, and whether an API key is present (never printed).
Exit code 0 = nothing blocking; 1 = at least one FAIL. WARN means a feature (live mode, recording, the page) cannot run yet.
"""
import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REQUIRED = [("numpy", "numpy"), ("pandas", "pandas")]
PAGE = [("streamlit", "streamlit"), ("plotly", "plotly")]
LIVE = [("google.genai", "google-genai")]


def _import_check(modules, level, why, importer):
    out = []
    for mod, pip_name in modules:
        try:
            m = importer(mod)
            out.append((f"package {pip_name}", "OK", getattr(m, "__version__", "installed")))
        except Exception:  # noqa: BLE001
            out.append((f"package {pip_name}", level, f"missing: {why}. Install with: pip install {pip_name}"))
    return out


def run_checks(importer=importlib.import_module, environ=None, root=ROOT):
    env = os.environ if environ is None else environ
    R = []
    v = sys.version_info
    R.append(("python >= 3.9", "OK" if v >= (3, 9) else "FAIL", f"{v.major}.{v.minor}.{v.micro}"))
    R += _import_check(REQUIRED, "FAIL", "the tests and the engine need it", importer)
    R += _import_check(PAGE, "WARN", "the Streamlit page cannot run", importer)
    R += _import_check(LIVE, "WARN", "recording and live mode cannot call Gemini", importer)
    key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
    R.append(("API key in the environment", "OK" if key else "WARN", "present (value hidden)" if key else "not set: needed only for demo.record; live mode asks for a key in the page"))
    try:
        from rag.build_benchmark import load_frozen
        from rag.build_hybrid_benchmark import BENCH_PATH, HASH_PATH
        from rag.build_hybrid_pipeline_spec import load_pipeline_spec
        from rag.corpus import load_corpus, verify_corpus
        _, h = load_frozen(BENCH_PATH, HASH_PATH)
        R.append(("hybrid benchmark matches its hash", "OK", h[:12]))
        _, ph = load_pipeline_spec()
        R.append(("pipeline spec and SQL agent code match their hashes", "OK", ph[:12]))
        corpus = load_corpus()
        R.append(("document corpus matches its fingerprint", "OK", verify_corpus(corpus)[:12]))
    except SystemExit as e:
        R.append(("frozen specifications intact", "FAIL", str(e)[:200]))
    except Exception as e:  # noqa: BLE001
        R.append(("frozen specifications intact", "FAIL", f"{type(e).__name__}: {str(e)[:160]}"))
    try:
        from analytics import export_bi
        files = export_bi.render()
        bad = [n for n, d in files.items() if not (Path(root) / "bi_data" / n).exists() or (Path(root) / "bi_data" / n).read_bytes() != d]
        R.append(("bi_data equals a fresh export", "OK" if not bad else "FAIL", "" if not bad else f"differs: {bad}. Line-ending conversion of the CSV files is the usual cause"))
    except SystemExit as e:
        R.append(("bi_data export reconciles", "FAIL", str(e)[:200]))
    except Exception as e:  # noqa: BLE001
        R.append(("bi_data export", "FAIL", f"{type(e).__name__}: {str(e)[:160]}"))
    try:
        from demo.engine import Engine
        from demo.trace import validate_trace
        eng = Engine()
        idx = json.loads((Path(root) / "traces" / "index.json").read_text(encoding="utf-8"))
        bad = []
        for e in idx["traces"]:
            t = json.loads((Path(root) / "traces" / e["file"]).read_text(encoding="utf-8"))
            problems = validate_trace(t, eng.docs)
            if problems:
                bad.append((e["trace_id"], problems[0][:80]))
        R.append(("shipped traces validate", "OK" if not bad else "FAIL", f"{len(idx['traces'])} traces" if not bad else str(bad)))
        R.append(("recordings still pending", "OK" if not idx["pending_recordings"] else "WARN",
                  "none" if not idx["pending_recordings"] else ", ".join(p["trace_id"] for p in idx["pending_recordings"]) + " (record them with demo.record)"))
    except Exception as e:  # noqa: BLE001
        R.append(("traces", "FAIL", f"{type(e).__name__}: {str(e)[:160]}"))
    return R


def main():
    results = run_checks()
    for name, state, detail in results:
        print(f"[{state:<4}] {name}" + (f"  -  {detail}" if detail else ""))
    fails = sum(s == "FAIL" for _, s, _ in results)
    warns = sum(s == "WARN" for _, s, _ in results)
    print(f"\n{fails} FAIL, {warns} WARN")
    print("Next: python -m demo.smoke_app   then   streamlit run demo/streamlit_app.py" if not fails else "Fix the FAIL lines first.")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
