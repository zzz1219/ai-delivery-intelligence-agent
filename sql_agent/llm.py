"""LLM adapters. The agent only needs complete(task, system, user) -> str."""
import os
import random
import re
import sys
import time


class AnthropicLLM:
    """Real model. Needs `pip install anthropic` and ANTHROPIC_API_KEY. Not exercised in the offline sandbox."""

    def __init__(self, model=None, max_tokens=1000):
        import anthropic  # lazy import
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens
        self.name = f"anthropic:{self.model}"

    def complete(self, task, system, user):
        resp = self.client.messages.create(model=self.model, max_tokens=self.max_tokens,
                                           system=system, messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


_TRANSIENT_CODES = frozenset({429, 503, 504})
_TRANSIENT_MARKERS = ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED")


def is_provider_transient(exc):
    """True only for explicit provider-side / network transient errors (rate limit, overload, timeout).
    Anything else (TypeError, KeyError, SDK interface changes, our own bugs) is NOT transient and must never
    be mistaken for 'the model is busy'."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in _TRANSIENT_CODES:
        return True
    name = type(exc).__name__
    if "Timeout" in name or "ConnectError" in name or "ConnectionError" in name:
        return True
    return any(m in str(exc) for m in _TRANSIENT_MARKERS)


class GeminiLLM:
    """Google Gemini via the google-genai SDK (`pip install google-genai`, key in GEMINI_API_KEY).
    The model id is deliberately not hard-coded: Gemini model names and free-tier eligibility change often,
    so pass model=... (CLI: --model) or set GEMINI_MODEL. Not exercised against the real API in the offline sandbox."""

    def __init__(self, model=None, client=None, max_tokens=8192, max_retries=4, sleep=time.sleep,
                 jitter=None):
        self.model = model or os.environ.get("GEMINI_MODEL")
        if not self.model:
            raise ValueError("Gemini model id required: pass --model or set GEMINI_MODEL "
                             "(see the model list in Google AI Studio)")
        if client is None:
            from google import genai  # lazy import
            client = genai.Client()   # reads GEMINI_API_KEY
        self.client = client
        self.max_tokens, self.max_retries, self.sleep = max_tokens, max_retries, sleep
        # max_retries counts retries AFTER the first call: up to 1 + 4 = 5 requests per completion
        self.jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self.name = f"gemini:{self.model}"

    def _config(self, system):
        from google.genai import types
        return types.GenerateContentConfig(system_instruction=system, max_output_tokens=self.max_tokens)

    def complete(self, task, system, user):
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=user,
                                                           config=self._config(system))
                text = (getattr(resp, "text", None) or "").strip()
                if not text:
                    raise RuntimeError("Gemini returned an empty response (blocked or token budget used up by thinking)")
                return text
            except Exception as e:  # noqa: BLE001
                if attempt < self.max_retries and is_provider_transient(e):
                    wait = min(60, 5 * 2 ** attempt) * self.jitter()   # about 5, 10, 20, 40 s (+-20%): ~75 s in total
                    print(f"[gemini] {type(e).__name__}: retry {attempt + 1}/{self.max_retries} in {wait:.0f}s",
                          file=sys.stderr, flush=True)
                    self.sleep(wait)
                    continue
                raise


class ScriptedLLM:
    """Returns queued SQL responses in order; used for tests (retry / failure paths)."""
    name = "scripted"

    def __init__(self, sql_responses, answer="ok"):
        self.queue = list(sql_responses)
        self.answer = answer

    def complete(self, task, system, user):
        if task == "sql":
            return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return self.answer


class OracleLLM:
    """Returns the reference SQL for each question. A pipeline sanity check, NOT a measure of agent accuracy."""
    name = "oracle"

    def __init__(self, question_to_sql):
        self.map = question_to_sql

    def complete(self, task, system, user):
        if task == "sql":
            q = re.search(r"Question: (.*)", user).group(1).strip()
            return f"```sql\n{self.map[q]}\n```"
        return "Result: " + user.split("Result:\n", 1)[-1]
