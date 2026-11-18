"""Parse unique, non-empty metadata fields from captured HTML bytes."""

from __future__ import annotations

from html.parser import HTMLParser

PRESENT = "present"
MISSING = "missing"
DUPLICATE = "duplicate"
EMPTY = "empty"
UNREADABLE = "unreadable"


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "meta":
            return
        values = dict(attrs)
        name = values.get("name")
        if name is None:
            return
        content = values.get("content")
        self.fields.setdefault(str(name).strip().lower(), []).append(
            "" if content is None else content
        )


def read_fields(body: bytes) -> dict[str, list[str]] | None:
    parser = _MetaParser()
    try:
        parser.feed(body.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    return parser.fields


def field_states(body: bytes, required: list[str]) -> tuple[dict[str, dict], bool]:
    """Per-field state; only exactly one non-empty value from this URL qualifies.

    A field can never be borrowed from another page: the only bytes read are the
    captured body named for this URL.
    """
    fields = read_fields(body)
    states: dict[str, dict] = {}
    for field in required:
        values = (fields or {}).get(field, [])
        if fields is None:
            state = UNREADABLE
        elif not values:
            state = MISSING
        elif len(values) > 1:
            state = DUPLICATE
        elif not values[0].strip():
            state = EMPTY
        else:
            state = PRESENT
        states[field] = {"state": state, "values": list(values)}
    return states, all(entry["state"] == PRESENT for entry in states.values())


def failed_fields(states: dict[str, dict]) -> list[dict[str, str]]:
    return [
        {"field": field, "state": entry["state"]}
        for field, entry in states.items()
        if entry["state"] != PRESENT
    ]
