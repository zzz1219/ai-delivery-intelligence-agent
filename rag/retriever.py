"""BM25 lexical retriever. Results are always document-level: {rank, doc_id, score, unit}."""
from .bm25 import BM25Index


class BM25Retriever:
    def __init__(self, corpus, granularity="document", k1=1.5, b=0.75):
        assert granularity in ("document", "section")
        self.granularity, self.k1, self.b = granularity, k1, b
        items, self.owner = [], {}
        for d in corpus:
            if granularity == "document":
                items.append((d.doc_id, d.text))
                self.owner[d.doc_id] = d.doc_id
            else:
                for i, (heading, text) in enumerate(d.sections):
                    uid = f"{d.doc_id}#{i}:{heading}"
                    items.append((uid, f"{d.title}\n{heading}\n{text}"))
                    self.owner[uid] = d.doc_id
        self.n_units = len(items)
        self.index = BM25Index(items, k1, b)
        self.name = f"bm25-{granularity}"

    def config(self):
        return dict(retriever="bm25", granularity=self.granularity, k1=self.k1, b=self.b,
                    units=self.n_units, aggregation="max chunk score per document")

    def search(self, query, k=10):
        seen = {}
        for uid, score in self.index.search(query, k=self.n_units):
            seen.setdefault(self.owner[uid], (score, uid))             # first hit = best chunk of that document
        ranked = sorted(seen.items(), key=lambda kv: (-kv[1][0], kv[0]))
        return [dict(rank=i + 1, doc_id=did, score=round(s, 4), unit=uid)
                for i, (did, (s, uid)) in enumerate(ranked[:k])]
