"""Run the real Streamlit page headlessly (no browser) with Streamlit's own AppTest and report any exception. Run from the delivery_agent folder:

    python -m demo.smoke_app

It opens the page in replay mode, selects every shipped trace in turn, and checks that nothing raised and that the expected tabs are drawn. It also
opens the live-mode form without running anything (no key, no model call). Needs streamlit >= 1.28 (AppTest). Not executed in the build sandbox.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
APP = ROOT / "demo" / "streamlit_app.py"


def main():
    try:
        from streamlit.testing.v1 import AppTest
    except Exception as e:  # noqa: BLE001
        print(f"streamlit's AppTest is not available ({type(e).__name__}). Run: pip install --upgrade streamlit   (needs >= 1.28)")
        raise SystemExit(2)
    idx = json.loads((ROOT / "traces" / "index.json").read_text(encoding="utf-8"))
    failures = []
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    if at.exception:
        print("FAIL first load:", [str(e.value)[:200] for e in at.exception])
        raise SystemExit(1)
    options = list(at.sidebar.selectbox[0].options)
    print(f"replay mode: {len(options)} trace(s) listed")
    for opt in options:
        at.sidebar.selectbox[0].set_value(opt)
        at.run()
        exc = [str(e.value)[:200] for e in at.exception]
        errs = [e.value[:120] for e in at.error]
        tabs = len(at.tabs)
        ok = not exc and not errs and tabs >= 5
        print(f"[{'OK' if ok else 'FAIL'}] {opt[:70]}  tabs={tabs} metrics={len(at.metric)} exceptions={len(exc)} st.error={len(errs)}")
        if not ok:
            failures.append((opt, exc or errs or f"only {tabs} tabs"))
    at.sidebar.radio[0].set_value("Live (your own API key)")
    at.run()
    live_ok = not at.exception and len(at.text_input) >= 2 and len(at.button) >= 1
    print(f"[{'OK' if live_ok else 'FAIL'}] live-mode form  text_inputs={len(at.text_input)} buttons={len(at.button)} exceptions={len(at.exception)}")
    if not live_ok:
        failures.append(("live mode", [str(e.value)[:200] for e in at.exception]))
    print(f"\n{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
