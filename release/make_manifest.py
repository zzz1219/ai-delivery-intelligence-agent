"""Writes the PRODUCTIZATION manifest: release evidence for the demo CORE (demo/*.py, viz/*.py, traces/*.json).

This is NOT a benchmark freeze. Benchmark fingerprints (benchmarks/*.sha256, rag.build_* checks) are untouched and are only quoted here.
Run from the delivery_agent folder:  python release/make_manifest.py --release v1.0-productized-core --tests 127
Never overwrites an existing manifest.
"""
import argparse
import hashlib
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS = ["demo/*.py", "viz/*.py", "traces/*.json"]   # core only: README/docs/assets are deliberately outside this fingerprint


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True)
    ap.add_argument("--tests", required=True, help="number of passing tests you observed")
    a = ap.parse_args()
    day = date.today().strftime("%Y%m%d")
    out = ROOT / "release" / f"productization_manifest_{day}.txt"
    if out.exists():
        sys.exit(f"{out.name} already exists; not overwriting")
    corpus = (ROOT / "benchmarks" / "corpus.sha256").read_text(encoding="utf-8").strip()
    lines = [f"release: {a.release}", f"date: {date.today().isoformat()}", f"tests: {a.tests} passed",
             f"corpus: {corpus}", "",
             "Productization fingerprint = release evidence of the final demo version.",
             "It does not re-freeze any benchmark; benchmark fingerprints are in benchmarks/*.sha256.", ""]
    for pattern in GROUPS:
        for p in sorted(ROOT.glob(pattern)):
            if p.is_file():
                lines.append(f"{sha(p)[:16]}  {p.relative_to(ROOT).as_posix()}")
    out.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    print("wrote", out.relative_to(ROOT))


if __name__ == "__main__":
    main()
