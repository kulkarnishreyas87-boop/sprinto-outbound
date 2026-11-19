"""S6 Choose contact: Hunter Domain Search (cached by domain) + code-only ranking.

Hunter quota is scarce: always check the cache first, and the pipeline only calls this
for companies that passed the gates (and, from milestone 5, tier A/B).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ContactsCfg
from .models import Contact
from .store import Store

HUNTER_URL = "https://api.hunter.io/v2/domain-search"


class HunterError(RuntimeError):
    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass
class HunterClient:
    api_key: str
    store: Store
    cfg: ContactsCfg
    client: httpx.Client | None = None
    cache_only: bool = False     # dry runs: never spend quota

    def domain_search(self, domain: str) -> tuple[dict[str, Any], bool]:
        """Return (response, called_api). Cached responses never hit the API."""
        cached = self.store.get_hunter(domain)
        if cached is not None:
            return cached, False
        if self.cache_only:
            raise HunterError("hunter_skipped_dry_run", "not in cache; dry runs make no Hunter calls")
        cap = self.cfg.hunter_monthly_cap
        if cap and self.store.hunter_calls_since(30) >= cap:
            raise HunterError("hunter_monthly_cap", f"{cap} searches already used in the last 30 days")
        if not self.api_key:
            raise HunterError("hunter_missing_key", "HUNTER_API_KEY is not set")
        h = self.cfg.hunter
        params = {"domain": domain, "limit": h.limit, "type": h.type}
        if h.department:
            params["department"] = ",".join(h.department)
        if h.seniority:
            params["seniority"] = ",".join(h.seniority)
        client = self.client or httpx.Client(timeout=20)
        try:
            # key in a header, never in the URL, so it can't leak into logs
            r = client.get(HUNTER_URL, params=params, headers={"X-API-KEY": self.api_key})
        except httpx.HTTPError as e:
            raise HunterError("hunter_unreachable", type(e).__name__) from e
        codes = {401: "hunter_auth", 403: "hunter_rate_limited", 429: "hunter_quota_exhausted"}
        if r.status_code != 200:
            detail = ""
            try:
                detail = "; ".join(e.get("id", "") for e in r.json().get("errors", []))
            except ValueError:
                pass
            raise HunterError(codes.get(r.status_code, f"hunter_http_{r.status_code}"), detail)
        data = r.json()
        self.store.log_hunter_call(domain)
        self.store.put_hunter(domain, data)   # cache successes, including empty results
        return data, True


# ---------- ranking (code only) ----------

_SYNONYMS = [
    (r"\bchief information security officer\b", "ciso"),
    (r"\bchief technology officer\b", "cto"),
    (r"\bchief technical officer\b", "cto"),
    (r"\bchief executive officer\b", "ceo"),
    (r"\bvice president\b", "vp"),
    (r"\bco[- ]?founder\b", "cofounder"),
    (r"\bgovernance,? risk,? (and |& )?compliance\b", "grc"),
]
_STOP = {"of", "the", "and", "for", "&"}


def norm_title(title: str) -> str:
    t = title.lower()
    for pat, rep in _SYNONYMS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(w for w in t.split() if w not in _STOP)


def title_has(title: str, keyword: str) -> bool:
    """Whole-word match after normalisation, so 'cto' never matches 'director'."""
    kw = norm_title(keyword).replace("technical cofounder", "cofounder")
    return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", norm_title(title)) is not None


def _rank_index(title: str, ranking: list[str]) -> int | None:
    for i, kw in enumerate(ranking):
        if title_has(title, kw):
            return i
    return None


def choose_contact(response: dict[str, Any], *, moment: str, has_open_security_role: bool,
                   cfg: ContactsCfg) -> tuple[Contact | None, str]:
    """Return (contact, reason_code_if_none). Records why the winner won and who else was considered."""
    emails = (response.get("data") or {}).get("emails") or []
    ranking = list(cfg.ranking.get(moment, []))
    if has_open_security_role:
        # prefer the likely hiring manager; never the role being hired (they don't exist yet)
        ranking = cfg.ranking.get("hiring_manager_for_open_role", []) + [k for k in ranking]
    candidates, considered = [], []
    for e in emails:
        name = " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x)
        title = e.get("position") or ""
        conf = e.get("confidence") or 0
        entry = {"name": name or None, "title": title or None, "confidence": conf}
        if not name or not e.get("value"):
            considered.append({**entry, "excluded": "no_name_or_email"})
            continue
        if not title:
            considered.append({**entry, "excluded": "no_title"})
            continue
        if any(title_has(title, kw) for kw in cfg.exclude_title_keywords):
            considered.append({**entry, "excluded": "excluded_function"})
            continue
        idx = _rank_index(title, ranking)
        if idx is None:
            considered.append({**entry, "excluded": "title_not_in_ranking"})
            continue
        candidates.append((conf < cfg.min_confidence, idx, -conf, e, name, title, entry))
    if not candidates:
        return None, "no_suitable_contact"
    candidates.sort(key=lambda c: c[:3])
    low_conf, idx, _, e, name, title, _ = candidates[0]
    why = (f"title '{title}' matches '{ranking[idx]}' (position {idx + 1} in the "
           f"{'hiring-manager-first ' if has_open_security_role else ''}{moment} ranking); "
           f"Hunter confidence {e.get('confidence')}"
           + (" (below preferred minimum)" if low_conf else ""))
    alternatives = [c[6] | {"rank": c[1] + 1} for c in candidates[1:]] + considered
    return Contact(name=name, title=title, email=e["value"], hunter_confidence=e.get("confidence"),
                   why_chosen=why, alternatives=alternatives), ""
