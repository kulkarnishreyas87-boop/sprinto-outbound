"""Load and validate versioned config (config/v{N}.yaml) and approved claims.

The active version lives in config/ACTIVE. Only `cli promote` writes it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
ACTIVE_FILE = CONFIG_DIR / "ACTIVE"

Effort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageModel(_Strict):
    model: str
    effort: Effort = "medium"
    max_tokens: int = 8000


class Price(_Strict):
    input: float
    output: float


class ModelsCfg(_Strict):
    provider: Literal["anthropic", "gemini"] = "anthropic"
    extract: StageModel
    support: StageModel
    qualify: StageModel
    pains: StageModel
    draft: StageModel
    judge: StageModel
    refusal_fallbacks: bool = True
    prices: dict[str, Price]

    @model_validator(mode="after")
    def _prices_cover_models(self) -> "ModelsCfg":
        for stage in (self.extract, self.support, self.qualify, self.pains, self.draft, self.judge):
            if stage.model not in self.prices:
                raise ValueError(f"no price configured for model {stage.model!r}")
        return self


class InputCfg(_Strict):
    recheck_after_days: int = 30


class ScrapeCfg(_Strict):
    user_agent: str
    delay_seconds: tuple[float, float]
    timeout_seconds: float
    render_fallback_min_words: int
    min_block_words: int
    max_tokens_per_page: int
    skip_path_sections: list[str] = Field(default_factory=list)
    page_keywords: dict[str, list[str]]


class ExtractionCfg(_Strict):
    required_fields: list[str]


class LoopCfg(_Strict):
    max_extra_fetches: int = Field(ge=0, le=4)  # brief: hard cap of 4
    page_map: dict[str, list[str]]


class JobBoardsCfg(_Strict):
    title_keywords: list[str]
    description_keywords: list[str]
    max_postings_in_text: int = 150


class DropIf(_Strict):
    b2c: bool = True
    agency_or_services: bool = True
    compliance_vendor: bool = True
    competitor_tool: bool = True


class GatesCfg(_Strict):
    competitor_tools: list[str]
    hosted_trust_markers: dict[str, list[str]] = Field(default_factory=dict)
    drop_if: DropIf

    @model_validator(mode="after")
    def _markers_name_competitors(self) -> "GatesCfg":
        unknown = set(self.hosted_trust_markers) - set(self.competitor_tools)
        if unknown:
            raise ValueError(f"hosted_trust_markers names tools not in competitor_tools: {sorted(unknown)}")
        return self


class StageProxies(_Strict):
    min_open_roles: int
    enterprise_tier: bool
    named_round: bool


class TiersCfg(_Strict):
    strong_signal_role_max_age_days: int
    in_progress_phrases: list[str]
    sensitive_data: list[str]
    stage_proxies: StageProxies


class HunterCfg(_Strict):
    limit: int = Field(default=10, ge=1, le=100)
    type: Literal["personal", "generic"] = "personal"
    department: list[str] = Field(default_factory=list)
    seniority: list[Literal["junior", "senior", "executive"]] = Field(default_factory=list)


class ContactsCfg(_Strict):
    hunter: HunterCfg = Field(default_factory=HunterCfg)
    min_confidence: int
    exclude_title_keywords: list[str]
    ranking: dict[str, list[str]]


class QACfg(_Strict):
    min_words: int
    max_words: int
    question_marks: int
    banned_phrases: list[str]
    subject_max_words: int = 7
    frameworks: list[str] = Field(default_factory=list)
    variants: int = Field(ge=1, le=3)
    regenerate_attempts: int = Field(ge=0, le=1)


class FeedbackCfg(_Strict):
    reason_codes: list[str]
    rewrite_edit_ratio: float
    min_occurrences_for_proposal: int


class Config(_Strict):
    version: int
    description: str = ""
    models: ModelsCfg
    input: InputCfg
    scrape: ScrapeCfg
    extraction: ExtractionCfg
    loop: LoopCfg
    jobboards: JobBoardsCfg
    gates: GatesCfg
    tiers: TiersCfg
    contacts: ContactsCfg
    qa: QACfg
    feedback: FeedbackCfg

    @model_validator(mode="after")
    def _page_map_refs_known_pages(self) -> "Config":
        known = set(self.scrape.page_keywords)
        for field, pages in self.loop.page_map.items():
            unknown = set(pages) - known
            if unknown:
                raise ValueError(f"page_map[{field}] references unknown pages {sorted(unknown)}")
        return self


class Claim(_Strict):
    id: str
    text: str
    approved: bool = False
    source: str = ""


class Claims(_Strict):
    claims: list[Claim]

    def approved(self) -> list[Claim]:
        return [c for c in self.claims if c.approved]


def config_path(version: int, config_dir: Path = CONFIG_DIR) -> Path:
    return config_dir / f"v{version}.yaml"


def active_version(config_dir: Path = CONFIG_DIR) -> int:
    f = config_dir / "ACTIVE"
    if not f.exists():
        return 1
    return int(f.read_text().strip().lstrip("v"))


def load_config(version: int | None = None, config_dir: Path = CONFIG_DIR) -> Config:
    v = version if version is not None else active_version(config_dir)
    path = config_path(v, config_dir)
    if not path.exists():
        raise FileNotFoundError(f"config version v{v} not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = Config.model_validate(data)
    if cfg.version != v:
        raise ValueError(f"{path.name} declares version {cfg.version}, expected {v}")
    return cfg


def load_claims(config_dir: Path = CONFIG_DIR) -> Claims:
    data = yaml.safe_load((config_dir / "claims.yaml").read_text(encoding="utf-8"))
    return Claims.model_validate(data)


class Settings(BaseModel):
    """Secrets and environment. Read from .env; never logged."""

    model_config = ConfigDict(frozen=True)

    my_email: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    hunter_api_key: str = ""
    resend_api_key: str = ""
    resend_from: str = "onboarding@resend.dev"

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return f"Settings(my_email={'set' if self.my_email else 'unset'}, keys=<redacted>)"

    __str__ = __repr__


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    return Settings(
        my_email=os.getenv("MY_EMAIL", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        hunter_api_key=os.getenv("HUNTER_API_KEY", ""),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        resend_from=os.getenv("RESEND_FROM", "onboarding@resend.dev").strip(),
    )
