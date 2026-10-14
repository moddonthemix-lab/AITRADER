"""The family's shared brain: decides cloud or local for every thought.

Copy this file (with hub_client.py) into an agent. One call does everything:

    from hub_client import HubClient
    from family_brain import FamilyBrain

    hub = HubClient()
    brain = FamilyBrain(hub, system="You are Scholar, the family's careful researcher. ...")
    t = brain.think("Find 3 ways small studios back up sessions", job="research",
                    local_only=message["local_only"])
    if t.brain == "wait":
        hub.update_task(task_id, "blocked", "Waiting for internet (research needs the web).")
    elif t.refused:
        ...  # the cloud model declined: tell the boss, don't retry elsewhere
    else:
        hub.brain = t.brain              # tags the post with ☁️ or 🏠
        hub.post_finding(..., t.text, ..., sources=t.sources)

How it decides, every time:
  1. 🏠 local-only channel            -> local model, always
  2. cloud possible? the anthropic SDK is installed, an API key is set, the
     internet is up, and the hub says the agent's budget allows it
       yes -> Claude (Haiku for chat, Sonnet for research and code, Opus for big
              approved jobs), cost reported to the hub
       no  -> research waits for internet/budget ("wait"); everything else uses
              the local model (Ollama)

Settings (environment or .env of the agent):
  ANTHROPIC_API_KEY   the family's API key (cloud mode only)
  LOCAL_MODEL         Ollama model, default qwen3:8b
  OLLAMA_URL          default http://localhost:11434

Cloud mode needs Python 3.10+ and `pip install anthropic`. Local mode needs only
the standard library and Ollama.

Prices below were current when this was written: check Anthropic's pricing page.
"""

import json
import os
import re
import socket
import urllib.error
import urllib.request

MODELS = {"chat": "claude-haiku-5-5", "research": "claude-sonnet-5-5",
          "code": "claude-sonnet-5-5", "big": "claude-opus-5-5"}
EFFORT = {"chat": "low", "research": "medium", "code": "medium", "big": "high"}
# USD per million tokens: (input, output).
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
HAIKU_LONG_PROMPT = (100_000, (0.50, 2.50))   # Haiku's price for prompts over 100K tokens
CACHE_WRITE, CACHE_READ = 1.25, 0.10          # multipliers on the input price
WEB_SEARCH_USD = 0.01                         # per web search (estimate; check current pricing)
WEB_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}   # models given the web search tool
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}  # server-side fallback (not Haiku)
MAX_TOKENS = 16000
EXPECTED_OUTPUT_TOKENS = 2000                 # for the "may I spend?" estimate
MAX_CONTINUES = 5                             # pause_turn continuations for long web research


class BrainUnavailable(Exception):
    """Neither cloud nor local could answer (e.g. Ollama isn't running and cloud isn't allowed)."""


class _CloudUnavailable(Exception):
    pass


class Thought:
    """One answer. brain: "cloud", "local" or "wait" (research must wait for internet or budget)."""

    def __init__(self, text="", brain="local", model=None, cost_usd=0.0, sources=None, refused=False, reason=""):
        self.text, self.brain, self.model, self.cost_usd = text, brain, model, cost_usd
        self.sources, self.refused, self.reason = sources or [], refused, reason

    def __repr__(self):
        return f"Thought(brain={self.brain!r}, model={self.model!r}, cost=${self.cost_usd:.4f}, refused={self.refused})"


def internet_ok(host="api.anthropic.com", timeout=2.0):
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


def price(model, input_tokens, output_tokens, cache_write=0, cache_read=0, web_searches=0):
    p_in, p_out = PRICES.get(model, PRICES["claude-opus-5-5"])  # unknown model: assume the dearer price
    if model == "claude-haiku-5-5" and input_tokens + cache_write + cache_read > HAIKU_LONG_PROMPT[0]:
        p_in, p_out = HAIKU_LONG_PROMPT[1]
    usd = (input_tokens * p_in + cache_write * p_in * CACHE_WRITE + cache_read * p_in * CACHE_READ
           + output_tokens * p_out) / 1_000_000
    return round(usd + web_searches * WEB_SEARCH_USD, 6)


class FamilyBrain:
    def __init__(self, hub=None, system="", local_model=None, ollama_url=None, cloud_client=None,
                 internet_check=internet_ok):
        """hub: a HubClient (for budgets; without it cloud is never used).
        cloud_client: an anthropic.Anthropic() to use (created automatically when needed)."""
        self.hub = hub
        self.system = system
        self.local_model = local_model or os.environ.get("LOCAL_MODEL", "qwen3:8b")
        self.ollama_url = (ollama_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self._client = cloud_client
        self._internet = internet_check

    # ------------------------------------------------------------------ the one call
    def think(self, prompt, job="chat", local_only=False, system=None, history=None, web=None):
        """Answer `prompt`. job: chat | research | code | big. history: [{"role", "content"}, ...]."""
        if job not in MODELS:
            raise ValueError(f"job must be one of: {', '.join(MODELS)}")
        system = system if system is not None else self.system
        web = (job == "research") if web is None else web
        if local_only:
            return self._local(prompt, system, history, "🏠 local-only channel")
        allowed, reason = self._cloud_allowed(prompt, system, history, job)
        if allowed:
            try:
                return self._cloud(prompt, system, history, job, web)
            except _CloudUnavailable as e:
                reason = str(e)
        if job == "research" and web:
            return Thought(brain="wait", reason=reason)
        return self._local(prompt, system, history, reason)

    # ------------------------------------------------------------------ deciding
    def _cloud_allowed(self, prompt, system, history, job):
        if self.hub is None:
            return False, "no hub connection to check the budget"
        if self._client is None:
            try:
                import anthropic  # noqa: F401
            except ImportError:
                return False, "cloud mode needs `pip install anthropic` (Python 3.10+)"
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                return False, "no ANTHROPIC_API_KEY set"
        if not self._internet():
            return False, "no internet"
        chars = len(prompt) + len(system or "") + sum(len(str(m.get("content", ""))) for m in history or [])
        estimate = price(MODELS[job], chars // 3, EXPECTED_OUTPUT_TOKENS)
        try:
            check = self.hub.budget_check(estimate)
        except Exception as e:  # hub unreachable or refused: be safe and stay local
            return False, f"couldn't check the budget with the hub ({e})"
        return bool(check.get("allowed")), check.get("reason", "")

    # ------------------------------------------------------------------ cloud
    def _cloud(self, prompt, system, history, job, web):
        try:
            import anthropic
        except ImportError:  # only possible when a client was passed in (e.g. tests)
            anthropic = None
        client = self._client or anthropic.Anthropic()
        self._client = client
        model = MODELS[job]
        messages = list(history or []) + [{"role": "user", "content": prompt}]
        kwargs = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages,
                  "output_config": {"effort": EFFORT[job]}}
        if system:
            kwargs["system"] = system
        if web and model in WEB_MODELS:
            kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
        totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "web": 0}
        served_by = model
        try:
            for _ in range(MAX_CONTINUES + 1):
                if model in FALLBACK_MODELS:
                    # If the model declines, Anthropic re-runs it on a recommended fallback model.
                    resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                       fallbacks="default", **kwargs)
                else:
                    resp = client.messages.create(**kwargs)
                served_by = getattr(resp, "model", None) or model
                u = resp.usage
                totals["input"] += u.input_tokens or 0
                totals["output"] += u.output_tokens or 0
                totals["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
                totals["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
                stu = getattr(u, "server_tool_use", None)
                totals["web"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
                if resp.stop_reason != "pause_turn":
                    break
                # A long web research turn paused: send it back as-is and the server resumes it.
                kwargs["messages"] = kwargs["messages"] + [{"role": "assistant", "content": resp.content}]
        except Exception as e:
            if anthropic is None:
                raise
            if isinstance(e, anthropic.AuthenticationError):
                raise _CloudUnavailable("the API key was rejected") from None
            if isinstance(e, (anthropic.APIConnectionError, anthropic.RateLimitError)):
                raise _CloudUnavailable(f"cloud AI unavailable ({type(e).__name__})") from None
            if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
                raise _CloudUnavailable(f"cloud AI unavailable ({e.status_code})") from None
            raise
        cost = price(served_by if served_by in PRICES else model, totals["input"], totals["output"],
                     totals["cache_write"], totals["cache_read"], totals["web"])
        self._report(served_by, totals, cost, job)
        if resp.stop_reason == "refusal":
            # Don't retry on the local model: a decline is a decision, not an outage.
            details = getattr(resp, "stop_details", None)
            why = getattr(details, "explanation", None) or "the cloud model declined this request"
            return Thought(brain="cloud", model=served_by, cost_usd=cost, refused=True, reason=why)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return Thought(text=text, brain="cloud", model=served_by, cost_usd=cost, sources=self._sources(resp.content))

    def _report(self, model, totals, cost, job):
        try:
            self.hub.report_usage(model, totals["input"] + totals["cache_write"] + totals["cache_read"],
                                  totals["output"], cost, note=job)
        except Exception:  # the outbox keeps it if the hub is down; never lose the answer over this
            pass

    @staticmethod
    def _sources(content):
        urls = []
        for b in content:
            if getattr(b, "type", "") == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
                for r in b.content:
                    url = getattr(r, "url", None)
                    if url and url not in urls:
                        urls.append(url)
        return urls

    # ------------------------------------------------------------------ local
    def _local(self, prompt, system, history, reason):
        messages = ([{"role": "system", "content": system}] if system else []) + list(history or []) + \
                   [{"role": "user", "content": prompt}]
        body = json.dumps({"model": self.local_model, "messages": messages, "stream": False}).encode()
        req = urllib.request.Request(f"{self.ollama_url}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:  # CPU-only models can be slow
                out = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise BrainUnavailable(f"local model not reachable at {self.ollama_url}: is Ollama running? ({e}); "
                                   f"cloud not used because: {reason}") from None
        text = (out.get("message") or {}).get("content", "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # some models show their reasoning
        return Thought(text=text, brain="local", model=self.local_model, reason=reason)
