import json
from pathlib import Path
import yaml
from crawlbudget.site_resilience import preview_scenario_hardening


def test_replica_sites_match_loopback_demo_sites():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/site_resilience/intake.json").read_text())
    config=yaml.safe_load((root/"fixtures/demo_sites.yaml").read_text())
    assert set(data["sites"])=={s["site_id"] for s in config["sites"]}
    assert all(s["base_url"].startswith("http://127.0.0.1:") for s in config["sites"])


def test_preview_uses_two_incompatible_pre_outage_choices():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/site_resilience/intake.json").read_text())
    assert preview_scenario_hardening(data)=={"west-outage":"A","east-outage":"C"}
    assert data["harden_cost"]["B"]>data["harden_cost"]["A"]
