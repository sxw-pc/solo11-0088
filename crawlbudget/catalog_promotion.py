"""Independent per-cart promotional quote preview on verified local offers."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .catalog_offers import extract_offers,quote_basket
from .util import sha256_bytes


def preview_promotions(data: dict, catalog: dict) -> list[dict]:
    offers_by_cart={}
    for offer in data["offers"]:
        offers_by_cart.setdefault(offer["cart_id"],[]).append(offer)
    result=[]
    for cart in data["carts"]:
        quote=quote_basket([catalog],cart["items"])
        if not quote["fulfillable"]:continue
        offer=sorted(offers_by_cart[cart["cart_id"]],key=lambda o:(-o["uplift_points"],o["offer_id"]))[0]
        net=quote["total_cents"]-offer["discount_cents"]
        fee=data["shipping"]["fee_cents"] if net<data["shipping"]["free_threshold_cents"] else 0
        result.append({"cart_id":cart["cart_id"],"offer_id":offer["offer_id"],
                       "customer_payable_cents":net+fee,"uplift_points":offer["uplift_points"]})
    return result


def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/catalog_promotion/intake.json"))
    args=p.parse_args(argv)
    data=json.loads(args.fixture.read_text())
    body=Path(data["source_page_path"]).read_bytes()
    event={"run_id":"promotion-demo","site_id":data["source_site"],"url_hash":"promotion-catalog",
           "outcome":"success","body_sha256":sha256_bytes(body)}
    print(json.dumps(preview_promotions(data,extract_offers(body,event)),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
