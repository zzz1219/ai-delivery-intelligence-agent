"""Dense retriever: sentence embeddings + cosine similarity in plain NumPy (71 documents, no vector database).

The embedder is injected, so tests run without downloading a model. The real embedder is
SentenceTransformerEmbedder (pip install sentence-transformers; the model is downloaded on first use).
"""
import numpy as np


class SentenceTransformerEmbedder:
    def __init__(self, model_name, batch_size=32):
        try:
            import sentence_transformers
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise SystemExit("Dense retrieval needs sentence-transformers:  pip install sentence-transformers  "
                             "(the first run downloads the model, about 130 MB).")
        self.model_name, self.batch_size = model_name, batch_size
        self.model = SentenceTransformer(model_name)
        self.library_version = sentence_transformers.__version__

    def encode(self, texts):
        return np.asarray(self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                            show_progress_bar=False), dtype=float)


def _normalize(m):
    m = np.asarray(m, dtype=float)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.where(norms == 0, 1.0, norms)


class DenseRetriever:
    def __init__(self, corpus, embedder, granularity="document", query_prefix="", model_name=None):
        assert granularity in ("document", "section")
        self.embedder, self.granularity, self.query_prefix = embedder, granularity, query_prefix
        self.model_name = model_name or getattr(embedder, "model_name", "custom")
        self.ids, self.owner, texts = [], {}, []
        for d in corpus:
            if granularity == "document":
                self.ids.append(d.doc_id)
                self.owner[d.doc_id] = d.doc_id
                texts.append(d.text)
            else:
                for i, (heading, text) in enumerate(d.sections):
                    uid = f"{d.doc_id}#{i}:{heading}"
                    self.ids.append(uid)
                    self.owner[uid] = d.doc_id
                    texts.append(f"{d.title}\n{heading}\n{text}")
        self.vectors = _normalize(embedder.encode(texts))          # documents are embedded WITHOUT the query prefix
        self.name = f"dense-{granularity}"

    def config(self):
        return dict(retriever="dense", model=self.model_name, granularity=self.granularity,
                    query_prefix=self.query_prefix, similarity="cosine", normalized=True,
                    library_version=getattr(self.embedder, "library_version", None), units=len(self.ids),
                    aggregation="max similarity per document")

    def search(self, query, k=10):
        q = _normalize(self.embedder.encode([self.query_prefix + query]))[0]
        sims = self.vectors @ q
        order = sorted(range(len(self.ids)), key=lambda i: (-sims[i], self.ids[i]))
        seen = {}
        for i in order:
            seen.setdefault(self.owner[self.ids[i]], (float(sims[i]), self.ids[i]))
        ranked = sorted(seen.items(), key=lambda kv: (-kv[1][0], kv[0]))
        return [dict(rank=n + 1, doc_id=did, score=round(s, 4), unit=uid) for n, (did, (s, uid)) in enumerate(ranked[:k])]
