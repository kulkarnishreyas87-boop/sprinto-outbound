"""GeminiProvider with a fake google-genai client (no network)."""
from types import SimpleNamespace

import pytest

from sprinto.config import Settings
from sprinto.draft import DraftOutput
from sprinto.extract import ExtractOutput
from sprinto.llm import GeminiProvider, LLMError, inline_schema, make_provider

OK_JSON = '{"subject": "s", "body": "b", "hook_field": "h", "claim_id": "c"}'


def _resp(text, finish="STOP", block=None, tin=1000, tout=200, thoughts=300):
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
        prompt_feedback=SimpleNamespace(block_reason=block) if block else None,
        usage_metadata=SimpleNamespace(prompt_token_count=tin, candidates_token_count=tout,
                                       thoughts_token_count=thoughts),
    )


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def provider(cfg, store, responses):
    models = FakeModels(responses)
    return GeminiProvider(cfg, store, "", client=SimpleNamespace(models=models)), models


def test_uses_config_model_thinking_and_schema(cfg, store):
    p, models = provider(cfg, store, [_resp(OK_JSON)])
    res = p.complete_json("draft", "sys", "user", DraftOutput)
    assert res.parsed.subject == "s"
    call = models.calls[0]
    assert call["model"] == cfg.models.draft.model
    assert call["config"].thinking_config.thinking_level.lower() in (cfg.models.draft.effort,) or \
        str(call["config"].thinking_config.thinking_level).lower().endswith(cfg.models.draft.effort)
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].system_instruction == "sys"


def test_thinking_tokens_billed_as_output(cfg, store):
    p, _ = provider(cfg, store, [_resp(OK_JSON, tin=1000, tout=200, thoughts=300)])
    res = p.complete_json("draft", "s", "u", DraftOutput)
    price = cfg.models.prices[cfg.models.draft.model]
    assert res.tokens_out == 500
    assert res.usd == pytest.approx((1000 * price.input + 500 * price.output) / 1e6)


def test_invalid_json_retried_once_then_fails(cfg, store):
    p, models = provider(cfg, store, [_resp("not json"), _resp('{"subject": 1}'), _resp(OK_JSON)])
    with pytest.raises(LLMError) as e:
        p.complete_json("draft", "s", "u", DraftOutput)
    assert e.value.reason_code == "llm_invalid_output" and len(models.calls) == 2


def test_retry_recovers(cfg, store):
    p, _ = provider(cfg, store, [_resp('{"truncated', finish="MAX_TOKENS"), _resp(OK_JSON)])
    assert p.complete_json("draft", "s", "u", DraftOutput).parsed.claim_id == "c"


@pytest.mark.parametrize("kw", [{"finish": "SAFETY"}, {"block": "PROHIBITED_CONTENT"}])
def test_safety_block_is_refusal(cfg, store, kw):
    p, _ = provider(cfg, store, [_resp("", **kw)])
    with pytest.raises(LLMError) as e:
        p.complete_json("draft", "s", "u", DraftOutput)
    assert e.value.reason_code == "llm_refusal"


def test_calls_are_logged_with_cost(cfg, store):
    p, _ = provider(cfg, store, [_resp("bad"), _resp(OK_JSON)])
    p.complete_json("extract", "s", "u", DraftOutput, record_id="r1")
    rows = store.conn.execute("SELECT ok, usd FROM llm_calls WHERE record_id='r1' ORDER BY id").fetchall()
    assert [r["ok"] for r in rows] == [0, 1] and all(r["usd"] > 0 for r in rows)


def test_inline_schema_has_no_refs_or_defs():
    import json
    schema = inline_schema(ExtractOutput)
    s = json.dumps(schema)
    assert "$ref" not in s and "$defs" not in s
    assert "title" not in schema                      # schema-level title label removed
    roles = schema["properties"]["compliance_security_roles"]["properties"]["value"]
    item = next(o for o in roles["anyOf"] if o.get("type") == "array")["items"]
    assert set(item["properties"]) == {"title", "posted_date", "url"}   # field named "title" kept
    assert "title" not in item["properties"]["title"]                  # but its label removed


def test_factory_picks_provider_from_config(cfg, store):
    assert cfg.models.provider == "gemini"
    with pytest.raises(LLMError) as e:
        make_provider(cfg, store, Settings())
    assert "GEMINI_API_KEY" in e.value.detail
