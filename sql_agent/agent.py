"""SQL agent: inspect schema -> generate SQL -> check -> read-only execute -> answer, with bounded retries.

Nodes are plain functions returning partial state updates, so the same nodes run in a simple loop
(run_agent, used by tests and evaluation) or inside LangGraph (build_langgraph).
"""
import re
from functools import partial
from typing import TypedDict

from .prompts import ANSWER_SYSTEM, SQL_SYSTEM, build_answer_user_prompt, build_sql_user_prompt
from .tools import DB_PATH, check_query, execute_query, get_schema_text

MAX_RETRIES = 2  # up to 3 generation attempts in total


class AgentState(TypedDict, total=False):
    question: str
    schema: str
    sql: str
    attempts: int
    last_sql: str
    last_error: str
    check_ok: bool
    result: dict
    answer: str
    status: str          # "ok" | "failed"
    trace: list


def extract_sql(text):
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    return (m.group(1) if m else text).strip()


def node_inspect_schema(state, db_path=DB_PATH):
    return {"schema": get_schema_text(db_path),
            "trace": state["trace"] + [{"node": "inspect_schema"}]}


def node_generate_sql(state, llm):
    raw = llm.complete("sql", SQL_SYSTEM, build_sql_user_prompt(
        state["question"], state["schema"], state.get("last_sql"), state.get("last_error")))
    sql = extract_sql(raw)
    return {"sql": sql, "attempts": state["attempts"] + 1,
            "trace": state["trace"] + [{"node": "generate_sql", "attempt": state["attempts"] + 1, "sql": sql}]}


def node_check_sql(state, db_path=DB_PATH):
    chk = check_query(state["sql"], db_path)
    update = {"check_ok": chk["ok"], "sql": chk["sql"],
              "trace": state["trace"] + [{"node": "check_sql", "ok": chk["ok"], "error": chk["error"]}]}
    if not chk["ok"]:
        update.update(last_sql=state["sql"], last_error=chk["error"])
    return update


def node_execute_sql(state, db_path=DB_PATH):
    res = execute_query(state["sql"], db_path)
    update = {"result": res, "trace": state["trace"] + [
        {"node": "execute_sql", "ok": res["ok"], "error": res["error"], "rows": len(res["rows"])}]}
    if not res["ok"]:
        update.update(last_sql=state["sql"], last_error=res["error"])
    return update


def node_answer(state, llm):
    r = state["result"]
    text = llm.complete("answer", ANSWER_SYSTEM, build_answer_user_prompt(
        state["question"], state["sql"], r["columns"], r["rows"], r["truncated"]))
    return {"answer": text.strip(), "status": "ok", "trace": state["trace"] + [{"node": "answer"}]}


def node_fail(state):
    return {"answer": f"Could not produce a valid query after {state['attempts']} attempts. Last error: {state.get('last_error')}",
            "status": "failed", "trace": state["trace"] + [{"node": "fail"}]}


def route_after_check(state):
    if state["check_ok"]:
        return "execute"
    return "retry" if state["attempts"] <= MAX_RETRIES else "fail"


def route_after_execute(state):
    if state["result"]["ok"]:
        return "answer"
    return "retry" if state["attempts"] <= MAX_RETRIES else "fail"


def run_agent(question, llm, db_path=DB_PATH):
    state = {"question": question, "attempts": 0, "trace": [], "status": "running"}
    state.update(node_inspect_schema(state, db_path))
    while True:
        state.update(node_generate_sql(state, llm))
        state.update(node_check_sql(state, db_path))
        route = route_after_check(state)
        if route == "execute":
            state.update(node_execute_sql(state, db_path))
            route = route_after_execute(state)
            if route == "answer":
                state.update(node_answer(state, llm))
                return state
        if route == "fail":
            state.update(node_fail(state))
            return state


def build_langgraph(llm, db_path=DB_PATH):
    """Same nodes wired as a LangGraph state machine. Requires `pip install langgraph`.
    NOTE: not executed in the offline sandbox (langgraph is not installed there). Node names must differ from
    state keys, hence 'write_answer' rather than 'answer'."""
    from langgraph.graph import END, StateGraph
    g = StateGraph(AgentState)
    g.add_node("inspect_schema", partial(node_inspect_schema, db_path=db_path))
    g.add_node("generate_sql", partial(node_generate_sql, llm=llm))
    g.add_node("check_sql", partial(node_check_sql, db_path=db_path))
    g.add_node("execute_sql", partial(node_execute_sql, db_path=db_path))
    g.add_node("write_answer", partial(node_answer, llm=llm))
    g.add_node("fail", node_fail)
    g.set_entry_point("inspect_schema")
    g.add_edge("inspect_schema", "generate_sql")
    g.add_edge("generate_sql", "check_sql")
    g.add_conditional_edges("check_sql", route_after_check,
                            {"execute": "execute_sql", "retry": "generate_sql", "fail": "fail"})
    g.add_conditional_edges("execute_sql", route_after_execute,
                            {"answer": "write_answer", "retry": "generate_sql", "fail": "fail"})
    g.add_edge("write_answer", END)
    g.add_edge("fail", END)
    return g.compile()
