"""Tests for the pre-flight check (the Streamlit smoke runner itself needs streamlit and is not run in the build sandbox).
Run from the delivery_agent folder:  python -m demo.test_preflight"""
import ast
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from . import preflight

ROOT = Path(__file__).resolve().parent.parent


def _by(results):
    return {n: (s, d) for n, s, d in results}


def test_missing_packages_are_reported_with_the_fix_and_the_right_severity():
    def importer(name):
        if name in ("streamlit", "plotly", "google.genai"):
            raise ModuleNotFoundError(name)
        return __import__(name)
    r = _by(preflight.run_checks(importer=importer, environ={}))
    assert r["package streamlit"][0] == "WARN" and "pip install streamlit" in r["package streamlit"][1]
    assert r["package plotly"][0] == "WARN" and r["package google-genai"][0] == "WARN"
    assert r["package numpy"][0] == "OK" and r["package pandas"][0] == "OK"
    def broken(name):
        if name == "numpy":
            raise ModuleNotFoundError(name)
        return __import__(name)
    assert _by(preflight.run_checks(importer=broken, environ={}))["package numpy"][0] == "FAIL"            # needed by the tests and the engine


def test_the_api_key_is_detected_but_never_printed():
    secret = "AIza" + "S" * 35
    r = preflight.run_checks(environ={"GEMINI_API_KEY": secret})
    assert _by(r)["API key in the environment"] == ("OK", "present (value hidden)")
    assert secret not in json.dumps(r)
    assert _by(preflight.run_checks(environ={"GOOGLE_API_KEY": secret}))["API key in the environment"][0] == "OK"
    assert _by(preflight.run_checks(environ={}))["API key in the environment"][0] == "WARN"
    buf = io.StringIO()
    old = preflight.run_checks
    preflight.run_checks = lambda: old(environ={"GEMINI_API_KEY": secret})
    try:
        with redirect_stdout(buf):
            try:
                preflight.main()
            except SystemExit:
                pass
    finally:
        preflight.run_checks = old
    assert secret not in buf.getvalue()


def test_the_intact_package_has_no_fail_and_no_recordings_pending():
    r = _by(preflight.run_checks(environ={}))
    assert all(s != "FAIL" for s, _ in r.values()), {k: v for k, v in r.items() if v[0] == "FAIL"}
    assert r["hybrid benchmark matches its hash"][0] == "OK" and r["pipeline spec and SQL agent code match their hashes"][0] == "OK"
    assert r["document corpus matches its fingerprint"][0] == "OK" and r["bi_data equals a fresh export"][0] == "OK"
    assert r["shipped traces validate"] == ("OK", "9 traces") and r["recordings still pending"] == ("OK", "none")


def test_tampering_with_the_package_is_reported_as_a_failure():
    d = Path(tempfile.mkdtemp())
    (d / "bi_data").mkdir()
    (d / "traces").mkdir()
    (d / "bi_data" / "fact_incidents.csv").write_bytes(b"not the export")                    # an empty / altered bi_data folder
    (d / "traces" / "index.json").write_text(json.dumps(dict(traces=[], pending_recordings=[])), encoding="utf-8")
    r = _by(preflight.run_checks(environ={}, root=d))
    assert r["bi_data equals a fresh export"][0] == "FAIL" and "differs" in r["bi_data equals a fresh export"][1]
    assert r["shipped traces validate"][0] == "OK" and r["recordings still pending"] == ("OK", "none")


def test_the_smoke_runner_is_syntactically_valid_and_only_uses_the_public_apptest_api():
    src = (ROOT / "demo" / "smoke_app.py").read_text(encoding="utf-8")
    ast.parse(src)
    assert "from streamlit.testing.v1 import AppTest" in src and "AppTest.from_file" in src and "default_timeout" in src
    assert "GEMINI_API_KEY" not in src and "os.environ" not in src                           # the smoke run never needs or touches a key
    assert "Live (your own API key)" in src and "Replay" not in src.split("def main")[0]


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
