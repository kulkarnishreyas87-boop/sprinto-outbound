import shutil

import pytest
import yaml
from pydantic import ValidationError

from sprinto.config import CONFIG_DIR, load_claims, load_config
from sprinto.models import DecisionRecord, Status
from sprinto.store import Store


def test_v1_loads():
    cfg = load_config(1)
    assert cfg.version == 1
    assert cfg.loop.max_extra_fetches <= 4
    assert "Vanta" in cfg.gates.competitor_tools


def test_approved_claims_are_complete():
    for c in load_claims().approved():
        assert "TODO" not in c.text and c.source.strip(), c.id
        assert not any(ch.isdigit() for ch in c.text.replace("27001", "").replace("SOC 2", "")) or c.source, c.id


def _copy_cfg(tmp_path, mutate):
    data = yaml.safe_load((CONFIG_DIR / "v1.yaml").read_text(encoding="utf-8"))
    mutate(data)
    (tmp_path / "v1.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    shutil.copy(CONFIG_DIR / "claims.yaml", tmp_path / "claims.yaml")
    return tmp_path


def test_loop_cap_above_4_rejected(tmp_path):
    d = _copy_cfg(tmp_path, lambda c: c["loop"].__setitem__("max_extra_fetches", 5))
    with pytest.raises(ValidationError):
        load_config(1, d)


def test_unknown_page_in_page_map_rejected(tmp_path):
    d = _copy_cfg(tmp_path, lambda c: c["loop"]["page_map"].__setitem__("buyer_type", ["linkedin"]))
    with pytest.raises(ValidationError):
        load_config(1, d)


def test_unknown_key_rejected(tmp_path):
    d = _copy_cfg(tmp_path, lambda c: c.__setitem__("surprise", 1))
    with pytest.raises(ValidationError):
        load_config(1, d)


def test_version_mismatch_rejected(tmp_path):
    d = _copy_cfg(tmp_path, lambda c: c.__setitem__("version", 2))
    with pytest.raises(ValueError):
        load_config(1, d)


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    run_id = s.create_run(1, ["acme.com"])
    rec = DecisionRecord(record_id="r1", run_id=run_id, config_version=1, domain="acme.com")
    rec.finish(Status.dropped, "dropped:b2c")
    s.save_record(rec)
    got = s.get_record("r1")
    assert got.status == Status.dropped and got.reason_code == "dropped:b2c"
    assert s.researched_recently("acme.com", 30)
    assert not s.researched_recently("other.com", 30)
    s.put_hunter("acme.com", {"data": {"emails": []}})
    assert s.get_hunter("acme.com") == {"data": {"emails": []}}
    s.close()
