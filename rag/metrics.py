"""Retrieval metrics at document level. `ranked` is a list of doc ids, best first."""


def hit_at_k(ranked, relevant, k):
    return 1.0 if any(d in relevant for d in ranked[:k]) else 0.0


def recall_at_k_capped(ranked, relevant, k):
    """Share of the achievable relevant documents found: |relevant in top-k| / min(|relevant|, k).
    Capped because a question with 10 relevant documents can never reach recall 1.0 at k=3."""
    if not relevant:
        return 0.0
    return sum(d in relevant for d in ranked[:k]) / min(len(relevant), k)


def precision_at_k(ranked, relevant, k):
    return sum(d in relevant for d in ranked[:k]) / k


def mrr(ranked, relevant):
    for i, d in enumerate(ranked, 1):
        if d in relevant:
            return 1.0 / i
    return 0.0


def first_rank(ranked, relevant):
    return next((i for i, d in enumerate(ranked, 1) if d in relevant), None)


def recall_at_k_standard(ranked, relevant, k):
    """Standard Recall@k: |relevant in top k| / |relevant|. Not capped (see recall_at_k_capped)."""
    if not relevant:
        return 0.0
    return sum(d in relevant for d in ranked[:k]) / len(relevant)


def auroc(positive_scores, negative_scores):
    """P(random positive scores higher than random negative), ties count 0.5. 1.0 = perfect separation."""
    if not positive_scores or not negative_scores:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positive_scores for n in negative_scores)
    return wins / (len(positive_scores) * len(negative_scores))
