"""Solo local collection-session quote preview for synthetic subscribers."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def solo_quotes(data: dict) -> dict[str, int]:
    depot=data["depot"]
    dist=data["navigation_cost"]
    return {s["subscriber_id"]:dist[depot][s["stop"]]+dist[s["stop"]][depot]
            for s in data["subscribers"]}


def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/subscriber_billing/intake.json"))
    args=p.parse_args(argv)
    print(json.dumps(solo_quotes(json.loads(args.fixture.read_text())),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
