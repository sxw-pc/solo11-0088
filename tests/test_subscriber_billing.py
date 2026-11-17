import hashlib
import json
from pathlib import Path
import yaml
from crawlbudget.subscriber_billing import solo_quotes


def test_subscriber_pages_and_sites_are_real_local_fixtures():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/subscriber_billing/intake.json").read_text())
    config=yaml.safe_load((root/"fixtures/demo_sites.yaml").read_text())
    assert {s["site_id"] for s in data["subscribers"]}=={s["site_id"] for s in config["sites"]}
    for s in data["subscribers"]:
        body=(root/s["page_path"]).read_bytes()
        assert len(body)==s["body_bytes"]
        assert hashlib.sha256(body).hexdigest()==s["body_sha256"]


def test_individual_quotes_exceed_feasible_shared_sessions():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/subscriber_billing/intake.json").read_text())
    assert solo_quotes(data)=={"subscriber-A":20,"subscriber-B":18,"subscriber-C":16}
    dist=data["navigation_cost"]
    assert sum(solo_quotes(data).values())==54
    assert dist["D"]["A"]+dist["A"]["B"]+dist["B"]["D"]+dist["D"]["C"]+dist["C"]["D"]==37
