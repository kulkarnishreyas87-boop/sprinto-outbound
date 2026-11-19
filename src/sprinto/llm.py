"""LLM provider abstraction. The model proposes; code decides.

Every call returns a pydantic-validated object. Invalid output is retried once, then
the stage fails with a reason code. Model names and effort come from config.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Config, StageModel
from .store import Store

M = TypeVar("M", bound=BaseModel)


class LLMError(RuntimeError):
    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass
class LLMResult(Generic[M]):
    parsed: M
    model: str
    tokens_in: int
    tokens_out: int
    usd: float


class LLMProvider(Protocol):
    def complete_json(self, stage: str, system: str, user: str, schema: type[M],
                      record_id: str | None = None) -> LLMResult[M]: ...


def estimate_usd(cfg: Config, model: str, tokens_in: int, tokens_out: int) -> float:
    p = cfg.models.prices.get(model)
    if p is None:
        return 0.0
    return (tokens_in * p.input + tokens_out * p.output) / 1_000_000


class AnthropicProvider:
    def __init__(self, cfg: Config, store: Store, api_key: str, client=None):
        self.cfg = cfg
        self.store = store
        if client is None:
            if not api_key:
                raise LLMError("missing_api_key", "ANTHROPIC_API_KEY is not set")
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)
        self.client = client

    def _stage(self, stage: str) -> StageModel:
        return getattr(self.cfg.models, stage)

    def complete_json(self, stage: str, system: str, user: str, schema: type[M],
                      record_id: str | None = None) -> LLMResult[M]:
        import anthropic

        sm = self._stage(stage)
        if sm.effort == "minimal":
            raise LLMError("bad_config", "effort 'minimal' is Gemini-only")
        total_in = total_out = 0
        last_err = ""
        for attempt in range(2):  # one retry on invalid output
            try:
                resp = self.client.messages.parse(
                    model=sm.model,
                    max_tokens=sm.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_format=schema,
                    output_config={"effort": sm.effort},
                )
            except (ValidationError, ValueError) as e:  # SDK failed to validate the JSON
                last_err = f"invalid_json: {e}"[:500]
                self._log(record_id, stage, sm.model, 0, 0, False, last_err)
                continue
            except anthropic.APIError as e:
                self._log(record_id, stage, sm.model, 0, 0, False, type(e).__name__)
                raise LLMError("llm_api_error", type(e).__name__) from e

            tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
            total_in += tin
            total_out += tout
            if resp.stop_reason == "refusal":
                self._log(record_id, stage, sm.model, tin, tout, False, "refusal")
                raise LLMError("llm_refusal", stage)
            if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
                last_err = f"incomplete_output (stop_reason={resp.stop_reason})"
                self._log(record_id, stage, sm.model, tin, tout, False, last_err)
                continue
            self._log(record_id, stage, sm.model, tin, tout, True)
            return LLMResult(resp.parsed_output, sm.model, total_in, total_out,
                             estimate_usd(self.cfg, sm.model, total_in, total_out))
        raise LLMError("llm_invalid_output", last_err)

    def _log(self, record_id, stage, model, tin, tout, ok, err=None):
        self.store.log_llm_call(record_id, stage, model, tin, tout,
                                estimate_usd(self.cfg, model, tin, tout), ok, err)


def inline_schema(schema: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $defs inlined and titles removed (simplest form for Gemini)."""
    raw = schema.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node, in_properties=False):
        if isinstance(node, dict):
            if not in_properties and "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            if in_properties:  # keys here are field names (e.g. a field called "title")
                return {k: walk(v) for k, v in node.items()}
            return {k: walk(v, in_properties=(k == "properties")) for k, v in node.items()
                    if k not in ("title", "default")}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(raw)


class GeminiProvider:
    """Gemini via google-genai `generate_content` with a JSON response schema."""

    _BAD_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}

    def __init__(self, cfg: Config, store: Store, api_key: str, client=None):
        self.cfg = cfg
        self.store = store
        if client is None:
            if not api_key:
                raise LLMError("missing_api_key", "GEMINI_API_KEY is not set")
            from google import genai

            client = genai.Client(api_key=api_key)
        self.client = client
        self._schemas: dict[type, dict] = {}

    def complete_json(self, stage: str, system: str, user: str, schema: type[M],
                      record_id: str | None = None) -> LLMResult[M]:
        from google.genai import errors, types

        sm: StageModel = getattr(self.cfg.models, stage)
        json_schema = self._schemas.setdefault(schema, inline_schema(schema))
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=sm.max_tokens,
            response_mime_type="application/json",
            response_json_schema=json_schema,
            thinking_config=types.ThinkingConfig(thinking_level=sm.effort),
        )
        total_in = total_out = 0
        last_err = ""
        for attempt in range(2):  # one retry on invalid output
            try:
                resp = self.client.models.generate_content(model=sm.model, contents=user, config=config)
            except errors.APIError as e:
                self._log(record_id, stage, sm.model, 0, 0, False, f"{type(e).__name__} {getattr(e, 'code', '')}")
                raise LLMError("llm_api_error", f"{type(e).__name__} {getattr(e, 'code', '')}") from e

            um = resp.usage_metadata
            tin = (um.prompt_token_count or 0) if um else 0
            tout = ((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0
            total_in += tin
            total_out += tout
            pf = getattr(resp, "prompt_feedback", None)
            finish = ""
            if resp.candidates:
                fr = resp.candidates[0].finish_reason
                finish = getattr(fr, "name", str(fr or ""))
            if (pf and pf.block_reason) or finish in self._BAD_FINISH:
                self._log(record_id, stage, sm.model, tin, tout, False, f"refusal:{finish or pf.block_reason}")
                raise LLMError("llm_refusal", f"{stage}: {finish or pf.block_reason}")
            try:
                parsed = schema.model_validate_json(resp.text or "")
            except (ValidationError, ValueError) as e:
                last_err = f"invalid_json (finish={finish}): {e}"[:500]
                self._log(record_id, stage, sm.model, tin, tout, False, last_err)
                continue
            self._log(record_id, stage, sm.model, tin, tout, True)
            return LLMResult(parsed, sm.model, total_in, total_out,
                             estimate_usd(self.cfg, sm.model, total_in, total_out))
        raise LLMError("llm_invalid_output", last_err)

    def _log(self, record_id, stage, model, tin, tout, ok, err=None):
        self.store.log_llm_call(record_id, stage, model, tin, tout,
                                estimate_usd(self.cfg, model, tin, tout), ok, err)


def make_provider(cfg: Config, store: Store, settings) -> LLMProvider:
    if cfg.models.provider == "gemini":
        return GeminiProvider(cfg, store, settings.gemini_api_key)
    return AnthropicProvider(cfg, store, settings.anthropic_api_key)
