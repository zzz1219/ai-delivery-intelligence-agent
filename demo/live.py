"""Live mode helpers: a bring-your-own-key Gemini client that never touches process-wide state, and a per-session usage limiter.

The key is passed to the client object directly. It is never written to os.environ (a hosted Streamlit process is shared by every visitor), never
stored in a trace and never logged.
"""
import math
import re

KEY_OK = re.compile(r"^\S{20,200}$")


def make_llm(model, api_key, client_factory=None):
    """GeminiLLM bound to THIS key only. client_factory(api_key) is injectable for tests; the default builds a google-genai client."""
    if not isinstance(api_key, str) or not KEY_OK.match(api_key.strip()):
        raise ValueError("enter your own Gemini API key (no spaces, 20 to 200 characters); it is used for this session only")
    if client_factory is None:
        def client_factory(key):
            from google import genai  # lazy import
            return genai.Client(api_key=key)
    from sql_agent.llm import GeminiLLM
    return GeminiLLM(model=model, client=client_factory(api_key.strip()))


class SessionLimiter:
    """At most `max_runs` live runs per session and `cooldown_s` seconds between runs (a refused attempt does not count)."""

    def __init__(self, max_runs=5, cooldown_s=20):
        self.max_runs, self.cooldown_s, self.runs, self.last = max_runs, cooldown_s, 0, None

    def allow(self, now):
        if self.runs >= self.max_runs:
            return False, f"this session has used its {self.max_runs} live runs; replay traces remain available"
        if self.last is not None and now - self.last < self.cooldown_s:
            return False, f"please wait {math.ceil(self.cooldown_s - (now - self.last))} more second(s) before the next run"
        self.runs += 1
        self.last = now
        return True, ""
