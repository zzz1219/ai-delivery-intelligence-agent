"""Two-lane retrieval: historical cases and guides are searched separately (BM25 over each lane), then combined.

Context order (pre-registered): cases first by rank, then guides by rank.
"""
from .retriever import BM25Retriever

GUIDE_TYPES = ("troubleshooting_guides", "deployment_guides")


class TwoLaneRetriever:
    def __init__(self, corpus, k_cases, k_guides, k1=1.5, b=0.75):
        self.k_cases, self.k_guides = k_cases, k_guides
        cases = [d for d in corpus if d.doc_type == "historical_cases"]
        guides = [d for d in corpus if d.doc_type in GUIDE_TYPES]
        self.case_lane = BM25Retriever(cases, "document", k1, b)
        self.guide_lane = BM25Retriever(guides, "document", k1, b)

    def retrieve_scored(self, query):
        return (self.case_lane.search(query, self.k_cases), self.guide_lane.search(query, self.k_guides))

    def retrieve(self, query):
        """Document ids in context order."""
        cases, guides = self.retrieve_scored(query)
        return [h["doc_id"] for h in cases] + [h["doc_id"] for h in guides]

    def config(self):
        return dict(retriever="bm25", granularity="document", lanes="cases / guides", k_cases=self.k_cases,
                    k_guides=self.k_guides, order="cases then guides")
