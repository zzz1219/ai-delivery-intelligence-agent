"""Plain-assert tests for the Hybrid benchmark specification. No model, no network.
Run from the delivery_agent directory:  python -m rag.test_hybrid_spec"""
import copy
import json
import re
import sqlite3

from . import build_hybrid_benchmark as bh
from .build_benchmark import load_frozen
from .corpus import load_corpus


def _spec():
    return load_frozen(bh.BENCH_PATH, bh.HASH_PATH)


def _conn():
    return sqlite3.connect(f"file:{(bh.ROOT / 'data/delivery.db').as_posix()}?mode=ro", uri=True)


def test_frozen_reproducible_and_structurally_sound():
    spec, h = _spec()
    assert spec["benchmark_version"] == "hybrid-1.0" and bh.build() == bh.BENCH_PATH.read_bytes()
    qs = spec["questions"]
    assert [q["id"] for q in qs] == ["H1", "H2", "H3", "H4", "H5"]
    assert all(q["expected_plan"] == {"requires_sql": True, "requires_retrieval": True, "retrieval_depends_on_sql": True} for q in qs)
    assert "expected_route" not in qs[0]
    assert ("not routing accuracy" in spec["stages"]["route"].lower() or "route compliance" in spec["stages"]["route"].lower())
    h4q = next(q for q in qs if q["id"] == "H4")
    assert "at least one relevant historical case" in h4q["question"] and "failure modes" not in h4q["question"]   # singular contract
    assert len({q["question"] for q in qs}) == 5                                   # no duplicate question texts
    for q in qs:
        assert q["sql_facts"] and q["doc_facts"] and q["oracle_queries"], q["id"]
        assert all(isinstance(b, str) for b in q["bonus_facts"])                  # bonus facts carry no scoring fields
    assert [q["id"] for q in qs if q.get("synthesis_requirements")] == ["H4", "H5"]
    assert set(spec["stages"]) == {"route", "sql", "retrieval", "synthesis"}
    assert "llm_calls" in spec["scoring"] and "not frozen" in spec["scoring"]["llm_calls"]
    assert any("ground_truth" in b for b in spec["information_boundary"])
    assert spec["pre_registered"]["selected"] == {"cases_per_query": 1, "guides_per_query": 2}


def test_gold_values_match_an_independent_recomputation():
    spec, _ = _spec()
    g = {f["id"]: f["gold"] for q in spec["questions"] for f in q["sql_facts"]}
    c = _conn()
    one = lambda sql, p=(): c.execute(sql, p).fetchone()
    hrs = "(julianday(resolved_at)-julianday(reported_at))*24"
    assert abs(g["H1-s1"] - one(f"SELECT AVG({hrs}) FROM incidents WHERE status='closed' AND category='database'")[0]) < 0.06
    assert abs(g["H1-s2"] - one(f"SELECT AVG({hrs}) FROM incidents WHERE status='closed' AND category<>'database'")[0]) < 0.06
    best = c.execute(f"SELECT root_cause, AVG({hrs}) m FROM incidents WHERE status='closed' GROUP BY 1 ORDER BY m DESC").fetchall()
    assert g["H1-s4"] == best[0][0] and abs(g["H1-s5"] - best[0][1]) < 0.06 and best[0][1] - best[1][1] > 3
    assert g["H1-s3"] is True and g["H1-s1"] > g["H1-s2"]
    assert (g["H2-s1"], g["H2-s2"]) == ("timeout", 25)
    n, mean = one(f"SELECT COUNT(*), AVG({hrs}) FROM incidents i JOIN deployments d USING(deployment_id) "
                  "WHERE i.severity='critical' AND d.env_type='distributed' AND i.status='closed'")
    assert g["H3-s2"] == n == 8 and abs(g["H3-s1"] - mean) < 0.06 and (g["H3-s3"], g["H3-s4"]) == ("timeout", 4)
    assert [g[f"H4-s{i}"] for i in (1, 2, 3, 4)] == [11, 40, 11, 14] and (g["H4-s5"], g["H4-s6"]) == ("v3.2", 32)
    assert (g["H5-s1"], g["H5-s2"]) == ("P03", 9) and g["H5-s3"] == ["INC_0185", "INC_0188", "INC_0192", "INC_0200"]


def test_questions_do_not_leak_gold_values():
    spec, _ = _spec()
    for q in spec["questions"]:
        text = q["question"]
        for f in q["sql_facts"]:
            for token in re.findall(r"[A-Za-z_]+|\d+(?:\.\d+)?", str(f["gold"])):
                if token in ("True", "False"):
                    continue
                assert not re.search(rf"(?<![\w.]){re.escape(token)}(?![\w.])", text), (q["id"], f["id"], token)


def test_evidence_is_single_item_supportable_and_verified_against_the_corpus():
    spec, _ = _spec()
    text = {d.doc_id: d.text for d in load_corpus()}
    for q in spec["questions"]:
        for f in q["doc_facts"]:
            if f["kind"] == "guide_fact":
                step = f["statement"].split(": ", 1)[1].rstrip(".")
                assert step in text[f["evidence_docs"][0]], f["id"]
            else:
                assert f["relevant_cases"] and all(bh._supports(f, text[d]) for d in f["relevant_cases"]), f["id"]
    h2 = next(f for q in spec["questions"] if q["id"] == "H2" for f in q["doc_facts"])
    assert len(h2["relevant_cases"]) >= 10 and "Keep timeouts consistent" in h2["statement"]


def test_sql_binding_taxonomy_and_h4_evidence_contract():
    spec, _ = _spec()
    b = spec["scoring"]["sql_binding"]
    ids = {f["id"] for q in spec["questions"] for f in q["sql_facts"]}
    assert "ONE SQL task" in b["scope"] and b["max_rows_for_numbers"] == 30
    assert all(set(pair) <= ids and len(pair) == 2 for pair in b["same_row"])            # every pair names real facts
    assert {"H1-s4", "H1-s5"} in [set(p) for p in b["same_row"]] and "confirmation" in b
    att = " ".join(spec["failure_attribution"]).lower()
    assert "query formulation" in att and "retriever / context" in att and "oracle" in att
    assert "oracle" in spec["stages"]["retrieval"].lower() and "never shown to the model" in spec["stages"]["retrieval"]
    assert "route compliance" in spec["scoring"]["route_metric"] and "routing accuracy" in spec["scoring"]["route_metric"]
    h4 = next(q for q in spec["questions"] if q["id"] == "H4")
    f = h4["doc_facts"][0]
    text = {d.doc_id: d.text for d in load_corpus()}
    assert not any("shortly after the upgrade" in m for m in f["evidence"]["any_of"])      # temporal remark is NOT sufficient
    assert all(m.startswith("Root cause: ") for m in f["evidence"]["any_of"])
    assert all(bh._supports(f, text[d]) for d in f["relevant_cases"]) and len(f["relevant_cases"]) >= 10
    # a case that only carries the temporal remark does not satisfy the fact
    assert not bh._supports(f, "The problem first appeared shortly after the upgrade to v3.2.")
    assert any("temporal evidence only" in b for b in h4["bonus_facts"])


def test_h5_is_a_rule_based_two_plus_two_design():
    spec, _ = _spec()
    h5 = next(q for q in spec["questions"] if q["id"] == "H5")
    s = h5["scored_incidents"]
    assert s["without_documented_case"] == ["INC_0188", "INC_0200"] and s["with_documented_case"] == ["INC_0185", "INC_0192"]
    assert len(h5["oracle_queries"]) == 4 and len(set(h5["oracle_queries"])) == 4          # distinct per-incident queries
    c = _conn()
    got = [r[0] for r in c.execute("SELECT incident_id FROM incidents WHERE status='open' AND severity='high' "
                                   "AND category='map_service' AND project_id='P03' ORDER BY 1")]
    assert got == sorted(s["without_documented_case"] + s["with_documented_case"])         # the SQL filter selects exactly these
    assert {f["incident_id"] for f in h5["doc_facts"]} == set(s["with_documented_case"])
    assert {y["incident_id"] for y in h5["synthesis_requirements"]} == set(s["without_documented_case"])


def test_reference_sql_runs_and_the_audit_rule_picks_the_smallest_sufficient_configuration():
    spec, _ = _spec()
    c = _conn()
    for name, sql in spec["sql_reference"].items():
        c.execute(sql, {"project_id": "P03"} if ":project_id" in sql else {}).fetchall()
    grid = spec["pre_registered"]["audit_grid"]
    ok = [r for r in grid if r["sufficient"]]
    best = min(ok, key=lambda r: (r["total"], r["mean_context_words"], r["cases_per_query"]))
    assert (best["cases_per_query"], best["guides_per_query"]) == (1, 2) and not any(r["sufficient"] for r in grid if r["total"] < 3)
    # the audit really can fail: with a nonsense oracle query no configuration is sufficient
    corpus = load_corpus()
    qs = copy.deepcopy(spec["questions"])
    qs[2]["oracle_queries"] = ["zzzz qqqq"]
    rows, pick = bh.audit(qs, corpus)
    assert pick is None and not any(r["sufficient"] for r in rows)


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
