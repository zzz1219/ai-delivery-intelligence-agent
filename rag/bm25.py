"""Okapi BM25 in pure Python (no dependencies, deterministic).

Tokenizer (pre-registered, no stemming): lowercase, [a-z0-9_]+ tokens; an identifier such as
connection_pool_exhausted yields the compound token AND its parts; a small English stopword list is removed.
"""
import math
import re
from collections import Counter

STOPWORDS = frozenset("a an and are as at be by for from has have in is it of on or that the this to was were "
                      "what when which with we our us do does did should can".split())
_TOKEN = re.compile(r"[a-z0-9_]+")


def tokenize(text):
    out = []
    for raw in _TOKEN.findall(text.lower()):
        raw = raw.strip("_")
        if not raw:
            continue
        parts = [p for p in raw.split("_") if p]
        if len(parts) > 1:
            out.append(raw)
        out.extend(parts)
    return [t for t in out if t not in STOPWORDS]


class BM25Index:
    def __init__(self, items, k1=1.5, b=0.75):
        """items: list of (unit_id, text)."""
        self.k1, self.b = k1, b
        self.ids = [i for i, _ in items]
        self.tf = [Counter(tokenize(t)) for _, t in items]
        self.dl = [sum(c.values()) for c in self.tf]
        self.N = len(items)
        self.avgdl = (sum(self.dl) / self.N) if self.N else 0.0
        self.df = Counter(term for c in self.tf for term in c)

    def idf(self, term):
        n = self.df.get(term, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))          # always >= 0

    def search(self, query, k=10):
        """Returns [(unit_id, score)] with score > 0, best first; ties broken by unit_id (deterministic)."""
        terms = set(tokenize(query))
        scored = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    s += self.idf(t) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl))
            if s > 0:
                scored.append((self.ids[i], s))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]
