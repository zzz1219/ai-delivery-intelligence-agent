SQL_SYSTEM = """You are a careful analytics engineer answering business questions about enterprise project delivery data.
Write exactly ONE SQLite SELECT statement that answers the question.
Rules:
- Use only the tables and columns in the schema. Never modify data.
- Return the statement in a single ```sql code block and nothing else.
- Select the columns needed to answer the question (identifier plus the measure), ordered sensibly.
- If a previous attempt failed, fix it according to the error message."""

ANSWER_SYSTEM = """You are an analytics assistant. Answer the question using ONLY the query result provided.
Rules:
- Be concise (2-4 sentences); quote numbers exactly as returned.
- State any assumption you relied on, for example the reference date for relative periods.
- Describe what the data shows; do not claim causes the data cannot prove.
- If the result was truncated, say so."""


def build_sql_user_prompt(question, schema, last_sql=None, last_error=None):
    text = f"Schema:\n{schema}\n\nQuestion: {question}\n"
    if last_sql and last_error:
        text += f"\nPrevious attempt:\n```sql\n{last_sql}\n```\nIt failed with: {last_error}\nWrite a corrected query.\n"
    return text


def build_answer_user_prompt(question, sql, columns, rows, truncated, max_rows=30):
    lines = [" | ".join(columns)] + [" | ".join(str(v) for v in r) for r in rows[:max_rows]]
    note = "\n(Result truncated.)" if truncated or len(rows) > max_rows else ""
    return f"Question: {question}\n\nSQL used:\n{sql}\n\nResult:\n" + "\n".join(lines) + note
