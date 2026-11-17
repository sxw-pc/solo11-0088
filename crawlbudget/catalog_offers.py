"""Read a small offline offer catalog from a successfully captured page body."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from .util import sha256_bytes


class _OfferParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.offers: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "article":
            return
        fields = dict(attrs)
        if "data-sku" not in fields:
            return
        required = ("data-sku", "data-title", "data-price-cents", "data-currency", "data-stock")
        if any(not fields.get(key) for key in required):
            raise ValueError("offer has missing catalog fields")
        price = int(fields["data-price-cents"] or "")
        stock = int(fields["data-stock"] or "")
        if price < 0 or stock < 0:
            raise ValueError("offer price and stock must be non-negative")
        self.offers.append({
            "sku": fields["data-sku"], "title": fields["data-title"],
            "price_cents": price, "currency": fields["data-currency"],
            "stock": stock,
        })


def extract_offers(body: bytes, request_event: dict[str, Any]) -> dict[str, Any]:
    """Extract rows only from the exact successful body represented by an event."""
    if request_event.get("outcome") != "success":
        raise ValueError("catalog extraction requires a successful capture")
    if sha256_bytes(body) != request_event.get("body_sha256"):
        raise ValueError("body does not match the capture event")
    parser = _OfferParser()
    parser.feed(body.decode("utf-8"))
    parser.close()
    skus = [offer["sku"] for offer in parser.offers]
    if len(skus) != len(set(skus)):
        raise ValueError("duplicate SKU in one page")
    return {
        "run_id": request_event["run_id"],
        "site_id": request_event["site_id"],
        "url_hash": request_event["url_hash"],
        "offers": parser.offers,
    }
