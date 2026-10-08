"""Free-question engine: runs the FROZEN pipeline (hybrid-pipeline-1.0) on any question and returns a trace.

Nothing of the evaluated pipeline is modified: the same prompts, the same SQL agent, the same two-lane BM25. A question that is not a benchmark
question has no oracle queries, so run_question gets an empty list and the diagnostic context stays empty.
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rag import hybrid_pipeline as hp  # noqa: E402
from rag.build_benchmark import load_frozen  # noqa: E402
from rag.build_hybrid_benchmark import BENCH_PATH, HASH_PATH  # noqa: E402
from rag.build_hybrid_pipeline_spec import load_pipeline_spec  # noqa: E402
from rag.corpus import load_corpus, verify_corpus  # noqa: E402
from rag.lanes import TwoLaneRetriever  # noqa: E402
from sql_agent.tools import DB_PATH  # noqa: E402

from .trace import build_trace, check_question  # noqa: E402


class Engine:
    def __init__(self, db_path=DB_PATH):
        _, self.hybrid_hash = load_frozen(BENCH_PATH, HASH_PATH)
        self.pspec, self.pspec_hash = load_pipeline_spec()
        self.corpus = load_corpus()
        self.corpus_sha = verify_corpus(self.corpus)
        self.docs = {d.doc_id: d for d in self.corpus}
        sel = self.pspec["retrieval"]
        self.retriever = TwoLaneRetriever(self.corpus, sel["cases_per_query"], sel["guides_per_query"])
        self.text = {k: d.text for k, d in self.docs.items()}
        self.db_path = db_path

    def specs(self):
        d = self.pspec["depends_on"]
        return dict(hybrid_benchmark_sha256=self.hybrid_hash, pipeline_spec_sha256=self.pspec_hash, corpus_sha256=self.corpus_sha,
                    sql_agent_code_sha256=d["sql_agent_code_sha256"])

    def answer(self, question, llm, *, trace_id, title=None, now=None, role=None):
        """Provider errors propagate to the caller (the UI shows them); a plan that is not valid is a result, not an error."""
        q = check_question(question)
        record = dict(id=trace_id, question=q, status="running", plan=None, plan_raw=None, tasks=[], queries=[], query_status=None, context_ids=[],
                      oracle_context_ids=[], answer=None)
        hp.run_question(dict(question=q, oracle_queries=[]), self.pspec, self.retriever, self.text, llm, self.db_path, record)
        return build_trace(record, trace_id=trace_id, kind="recorded_live", title=title or q, model=getattr(llm, "name", "llm"), specs=self.specs(),
                           docs_by_id=self.docs, recorded_at=(now or datetime.now()).isoformat(timespec="seconds"), role=role)
