import json
from pathlib import Path
from crawlbudget.catalog_promotion import preview_promotions
from crawlbudget.catalog_offers import extract_offers,quote_basket
from crawlbudget.util import sha256_bytes


def test_promotion_carts_quote_actual_captured_catalog():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/catalog_promotion/intake.json").read_text())
    body=(root/data["source_page_path"]).read_bytes()
    assert sha256_bytes(body)==data["source_body_sha256"]
    event={"run_id":"promo","site_id":data["source_site"],"url_hash":"catalog",
           "outcome":"success","body_sha256":sha256_bytes(body)}
    catalog=extract_offers(body,event)
    assert [quote_basket([catalog],cart["items"])["total_cents"] for cart in data["carts"]]==[4200,3000,2400]
    assert [q["offer_id"] for q in preview_promotions(data,catalog)]==["A","C","E"]


def test_independent_promotions_exceed_shared_budget():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/catalog_promotion/intake.json").read_text())
    assert sum(o["discount_cents"] for o in data["offers"] if o["offer_id"] in {"A","C","E"})==2500
    assert data["contracts"]["merchant_pays"]["merchant_cap_cents"]==1300
    assert 4200-1000+600-3200-400==200<300
