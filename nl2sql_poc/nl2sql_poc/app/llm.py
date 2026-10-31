"""
llm.py - the pluggable LLM layer.

Business logic (pipeline.py) only ever sees `LLMProvider.complete()`. Which vendor sits behind
it is decided by `llm.provider` in config.yaml, so a client can pick their own AI agent without
any code change. To add a vendor: subclass LLMProvider, implement complete(), register it in
PROVIDERS. Nothing else needs to know.

The interface is deliberately tiny: (system prompt, message list) -> text + token usage.
Prompt *quality* is NOT portable across vendors, so expect to re-tune prompt.py per provider.
"""
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("chatbot.llm")


def _log_completion(provider: str, model: str, started: float, response: "LLMResponse") -> None:
    log.info("LLM request completed provider=%s model=%s elapsed_ms=%d input_tokens=%d "
             "output_tokens=%d",
             provider, model, round((time.perf_counter() - started) * 1000),
             response.input_tokens, response.output_tokens)


def _log_failure(provider: str, model: str) -> None:
    log.exception("LLM request failed provider=%s model=%s", provider, model)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict = field(default_factory=dict)      # e.g. cache read/write tokens


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model = cfg["model"]

    @abstractmethod
    def complete(self, system: str, messages: list) -> LLMResponse:
        """messages: [{"role": "user" | "assistant", "content": str}, ...]"""


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, cfg):
        super().__init__(cfg)
        import anthropic                                   # lazy: only needed if selected
        # Reads ANTHROPIC_API_KEY from the environment. Never hard-code the key.
        self.client = anthropic.Anthropic()

    def complete(self, system, messages):
        started = time.perf_counter()
        kwargs = dict(model=self.model, max_tokens=self.cfg.get("max_tokens", 1500),
                      messages=messages)
        # The schema + data dictionary is a big, identical prefix on every request. Marking it
        # cacheable makes repeat calls cheaper/faster (only kicks in above the model's minimum
        # prompt size; below that it is silently ignored).
        if self.cfg.get("prompt_caching", True):
            kwargs["system"] = [{"type": "text", "text": system,
                                 "cache_control": {"type": "ephemeral"}}]
        else:
            kwargs["system"] = system
        # Optional: some models reject sampling parameters. Only sent if you set it in config.
        if self.cfg.get("temperature") is not None:
            kwargs["temperature"] = self.cfg["temperature"]

        try:
            resp = self.client.messages.create(**kwargs)
        except Exception:
            _log_failure(self.name, self.model)
            raise
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        result = LLMResponse(
            text=text, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            extra={"cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                   "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0})
        _log_completion(self.name, self.model, started, result)
        return result


class OpenAIProvider(LLMProvider):
    """Second vendor, to prove the layer is swappable. NOT yet tested against the live API."""
    name = "openai"

    def __init__(self, cfg):
        super().__init__(cfg)
        from openai import OpenAI                          # lazy: pip install openai
        self.client = OpenAI()                             # reads OPENAI_API_KEY

    def complete(self, system, messages):
        started = time.perf_counter()
        # No max_tokens / temperature passed on purpose: parameter names and support differ
        # between OpenAI model families, and the defaults are fine for a POC.
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=[{"role": "system", "content": system}] + messages)
        except Exception:
            _log_failure(self.name, self.model)
            raise
        result = LLMResponse(text=resp.choices[0].message.content or "",
                             input_tokens=resp.usage.prompt_tokens,
                             output_tokens=resp.usage.completion_tokens)
        _log_completion(self.name, self.model, started, result)
        return result


class GroqProvider(LLMProvider):
    """Groq provider using the OpenAI-compatible API."""

    name = "groq"

    def __init__(self, cfg):
        super().__init__(cfg)

        from openai import OpenAI

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY environment variable is not set.")

        # max_retries: the SDK backs off and honours Retry-After on HTTP 429 (per-minute token
        # limits on Groq's free/on-demand tiers are easy to hit with two calls per request).
        self.client = OpenAI(
            api_key=api_key,
            base_url=cfg.get("base_url", "https://api.groq.com/openai/v1"),
            max_retries=int(cfg.get("max_retries", 4)),
        )

    def complete(self, system, messages):
        started = time.perf_counter()
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            max_completion_tokens=int(self.cfg.get("max_tokens", 1500)),
        )
        # gpt-oss models spend completion tokens on hidden reasoning; "low" keeps enough budget
        # for the actual answer. Set llm.groq.reasoning_effort in the config (omit for other models).
        if self.cfg.get("reasoning_effort"):
            kwargs["extra_body"] = {"reasoning_effort": self.cfg["reasoning_effort"]}

        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception:
            _log_failure(self.name, self.model)
            raise
        choice = response.choices[0]
        text = choice.message.content or ""
        if not text.strip() and choice.finish_reason == "length":
            log.error("LLM response exhausted its output limit provider=%s model=%s",
                      self.name, self.model)
            raise RuntimeError("The model ran out of output tokens before answering "
                               "(raise llm.groq.max_tokens or lower reasoning_effort).")
        usage = response.usage
        result = LLMResponse(
            text=text,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )
        _log_completion(self.name, self.model, started, result)
        return result


PROVIDERS = {"claude": ClaudeProvider, "openai": OpenAIProvider, "groq": GroqProvider}



def build_llm(llm_cfg: dict) -> LLMProvider:
    name = llm_cfg["provider"]
    if name not in PROVIDERS:
        raise ValueError(f"Unknown llm.provider '{name}'. Available: {', '.join(PROVIDERS)}")
    return PROVIDERS[name](llm_cfg[name])
