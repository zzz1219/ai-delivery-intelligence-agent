"""Load the document corpus (historical cases + troubleshooting guides + deployment guides)."""
import argparse
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = ROOT / "docs"
DOC_TYPES = ("historical_cases", "troubleshooting_guides", "deployment_guides")


@dataclass
class Doc:
    doc_id: str                     # e.g. historical_cases/INC_0042
    doc_type: str
    path: Path
    title: str
    text: str
    metadata: dict = field(default_factory=dict)
    sections: list = field(default_factory=list)   # [(heading, text)], first heading is "overview"


def _split_sections(text):
    sections, heading, buf = [], "overview", []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((heading, "\n".join(buf).strip()))
            heading, buf = line[3:].strip().lower(), []
        else:
            buf.append(line)
    sections.append((heading, "\n".join(buf).strip()))
    return [(h, t) for h, t in sections if t]


def load_corpus(doc_dir=DOC_DIR):
    docs = []
    for doc_type in DOC_TYPES:
        for path in sorted((Path(doc_dir) / doc_type).glob("*.md")):
            text = path.read_text(encoding="utf-8")
            sections = _split_sections(text)
            overview = sections[0][1] if sections and sections[0][0] == "overview" else ""
            title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
            meta = {m.group(1).strip().lower().replace(" ", "_"): m.group(2).strip()
                    for m in (re.match(r"^- ([^:]+): (.*)$", l) for l in overview.splitlines()) if m}
            docs.append(Doc(f"{doc_type}/{path.stem}", doc_type, path, title, text, meta, sections))
    return docs


REGISTERED_PATH = ROOT / "benchmarks" / "corpus.sha256"


def corpus_fingerprint(docs):
    """SHA-256 over every document (sorted by id): id + text. Text is read in text mode, so CRLF/LF differences
    (e.g. after a Windows checkout) do not change it; any edit to a document does."""
    h = hashlib.sha256()
    for d in sorted(docs, key=lambda d: d.doc_id):
        h.update(d.doc_id.encode("utf-8") + b"\0" + d.text.encode("utf-8") + b"\0")
    return h.hexdigest()


def verify_corpus(docs, path=REGISTERED_PATH):
    """Refuse to run if the documents differ from the corpus that was registered when the benchmark was frozen."""
    fp = corpus_fingerprint(docs)
    if not Path(path).exists():
        raise SystemExit("no registered corpus fingerprint; register it once with:  python -m rag.corpus --register")
    expected = Path(path).read_text().split()[0]
    if fp != expected:
        raise SystemExit(f"the document corpus changed since it was registered (expected {expected[:12]}..., found "
                         f"{fp[:12]}...). BM25 and dense would no longer be compared on the same documents.")
    return fp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true", help="write benchmarks/corpus.sha256 (do this once, before runs)")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    docs = load_corpus()
    fp = corpus_fingerprint(docs)
    if args.register:
        REGISTERED_PATH.write_text(f"{fp}  corpus ({len(docs)} documents)\n", encoding="utf-8")
    if args.check:
        verify_corpus(docs)
    print(f"{len(docs)} documents, corpus sha256 {fp}")


if __name__ == "__main__":
    main()
