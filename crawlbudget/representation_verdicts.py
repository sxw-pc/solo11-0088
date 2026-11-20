"""Deterministic grouping and per-subject contract evaluation."""

from __future__ import annotations

from .representation import (
    BODY_FAILURE_REASONS,
    BODY_FILE_MISSING,
    CONFLICTING_CANDIDATES,
    EVIDENCE_FRESH_200,
    EVIDENCE_STRONG_304,
    EVIDENCE_UNPUBLISHED,
    EVIDENCE_WEAK_304,
    NO_CANDIDATE_200,
    ONLY_FAILED_GETS,
    VALIDATOR_MISMATCH,
    VALIDATOR_MISSING,
    VARY_HEADER_CONFLICT,
    VARY_WILDCARD,
    WEAK_NOT_BYTE_EXACT,
    BodyLocation,
    RepresentationFact,
    RepresentationVerdict,
    etag_strong_match,
    etag_weak_match,
    is_weak_etag,
    parse_vary,
    variant_signature,
)
from .representation_engine import fact_source, verified_candidate


def build_verdicts(
    facts: list[RepresentationFact],
    locations: dict[str, BodyLocation],
    failed_keys: frozenset[tuple[str, str]] = frozenset(),
) -> list[RepresentationVerdict]:
    groups: dict[tuple[str, str], list[RepresentationFact]] = {}
    for fact in facts:
        groups.setdefault((fact.site_id, fact.url), []).append(fact)
    verdicts: list[RepresentationVerdict] = []
    for site_id, url in sorted(groups):
        verdicts.extend(_build_for_subject(
            site_id, url, groups[(site_id, url)], locations,
            (site_id, url) in failed_keys,
        ))
    return verdicts


def _build_for_subject(
    site_id: str, url: str, facts: list[RepresentationFact],
    locations: dict[str, BodyLocation], has_failed_get: bool,
) -> list[RepresentationVerdict]:
    """Emit one verdict per Vary-selected request variant, in stable order.

    Bucketing projects request headers onto the Vary dimensions of each fact,
    so a 200 and its later conditional 304 (which adds ``If-None-Match``) share
    a variant while different ``Accept-Language`` values stay isolated. A
    wildcard or absent Vary collapses to one bucket whose 304 matcher enforces
    explicit per-header or wildcard rules.
    """
    ordered = sorted(facts, key=lambda item: (item.recorded_at, item.origin_id))
    buckets: dict[str, list[RepresentationFact]] = {}
    for fact in ordered:
        buckets.setdefault(_fact_variant_key(fact), []).append(fact)
    verdicts: list[RepresentationVerdict] = []
    for bucket_key in sorted(buckets):
        variant_facts = buckets[bucket_key]
        latest = variant_facts[-1]
        if latest.status == 200:
            vary = parse_vary(latest.vary)
            key = variant_signature(latest.request_headers, vary)
            verdicts.append(_verdict_for_200(site_id, url, latest, locations, key))
        else:
            verdicts.append(_one_304_verdict(
                site_id, url, latest,
                [fact for fact in variant_facts if fact.status == 200],
                locations, has_failed_get,
            ))
    return verdicts


def _fact_variant_key(fact: RepresentationFact) -> str:
    """Vary projection when Vary is present, else an empty-variant bucket.

    With no Vary header every response shares the empty projection; per-header
    isolation is enforced inside the 304 matcher rather than by bucketing, so
    the conditional ``If-None-Match`` header does not split a 304 from its
    prior 200.
    """
    vary_names = parse_vary(fact.vary)
    if vary_names and vary_names != ("*",):
        return variant_signature(fact.request_headers, vary_names)
    return ""


_CONDITIONAL_HEADERS = frozenset({"if-none-match"})


def _variant_headers(headers) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(
        (name, value) for name, value in headers
        if name not in _CONDITIONAL_HEADERS
    ))


def _same_request_variant(
    candidate: RepresentationFact, trigger: RepresentationFact,
    vary_names: tuple[str, ...],
) -> bool:
    if vary_names:
        return variant_signature(candidate.request_headers, vary_names) == \
            variant_signature(trigger.request_headers, vary_names)
    return _variant_headers(candidate.request_headers) == \
        _variant_headers(trigger.request_headers)


def _verdict_for_200(
    site_id: str, url: str, latest: RepresentationFact,
    locations: dict[str, BodyLocation], key: str,
) -> RepresentationVerdict:
    candidate = verified_candidate(latest, locations)
    assert candidate is not None
    chain = [fact_source(latest, locations)]
    if candidate["verified"]:
        contracts = {
            "semantic-readable": _contract(True, None),
            "byte-exact": _contract(True, None),
        }
        return RepresentationVerdict(
            site_id, url, key, 200, latest.recorded_at, latest.origin,
            latest.etag, latest.vary, EVIDENCE_FRESH_200,
            latest.body_sha256, candidate["body_path"], True, contracts,
            None, chain, [candidate],
        )
    reason = candidate["not_publishable_reason"] or BODY_FILE_MISSING
    return RepresentationVerdict(
        site_id, url, key, 200, latest.recorded_at, latest.origin,
        latest.etag, latest.vary, EVIDENCE_UNPUBLISHED,
        latest.body_sha256, None, False,
        {name: _contract(False, reason)
         for name in ("semantic-readable", "byte-exact")},
        reason, chain, [candidate],
    )


def _one_304_verdict(
    site_id: str, url: str, trigger: RepresentationFact,
    prior_200: list[RepresentationFact], locations: dict[str, BodyLocation],
    has_failed_get: bool,
) -> RepresentationVerdict:
    vary_names = parse_vary(trigger.vary)
    key = variant_signature(trigger.request_headers, vary_names)
    chain = [fact_source(trigger, locations)]
    if "*" in vary_names:
        return _blocked_304(site_id, url, key, trigger, VARY_WILDCARD, chain, [])
    same_vary: list[RepresentationFact] = []
    vary_conflict = False
    for fact in prior_200:
        fact_vary = parse_vary(fact.vary)
        if fact_vary == ("*",):
            vary_conflict = True
            continue
        if fact.vary.strip() and fact_vary != vary_names:
            vary_conflict = True
            continue
        same_vary.append(fact)
    # With named Vary dimensions, compare the projection. With no Vary header,
    # compare the full request-header sets so a zh 200 can never backfill an
    # en request (conditional validator headers are excluded from both sides).
    applicable = [
        fact for fact in same_vary if _same_request_variant(fact, trigger, vary_names)
    ]
    candidates = [
        row for row in (verified_candidate(fact, locations) for fact in applicable)
        if row is not None
    ]
    if not applicable:
        if vary_conflict:
            reason = VARY_HEADER_CONFLICT
        elif has_failed_get:
            reason = ONLY_FAILED_GETS
        else:
            reason = NO_CANDIDATE_200
        return _blocked_304(site_id, url, key, trigger, reason, chain, candidates)
    verified = [row for row in candidates if row["verified"]]
    if not verified:
        reason = next(
            (row["not_publishable_reason"] for row in candidates
             if row["not_publishable_reason"] in BODY_FAILURE_REASONS),
            NO_CANDIDATE_200,
        )
        return _blocked_304(site_id, url, key, trigger, reason, chain, candidates)
    distinct = {row["body_sha256"] for row in verified}
    if len(distinct) > 1:
        return _blocked_304(site_id, url, key, trigger, CONFLICTING_CANDIDATES,
                            chain, candidates)
    chosen = verified[-1]
    chosen_fact = next(
        fact for fact in applicable if fact.body_sha256 == chosen["body_sha256"]
    )
    chain.append(fact_source(chosen_fact, locations))
    return _validator_verdict(site_id, url, key, trigger, chosen_fact,
                              chosen, chain, candidates)


def _validator_verdict(
    site_id: str, url: str, key: str, trigger: RepresentationFact,
    chosen: RepresentationFact, candidate: dict[str, object],
    chain: list[dict[str, object]], candidates: list[dict[str, object]],
) -> RepresentationVerdict:
    if not trigger.etag:
        return _blocked_304(site_id, url, key, trigger, VALIDATOR_MISSING,
                            chain, candidates)
    if not chosen.etag or not etag_weak_match(trigger.etag, chosen.etag):
        return _blocked_304(site_id, url, key, trigger, VALIDATOR_MISMATCH,
                            chain, candidates)
    if is_weak_etag(trigger.etag) or is_weak_etag(chosen.etag):
        contracts = {
            "semantic-readable": _contract(True, None),
            "byte-exact": _contract(False, WEAK_NOT_BYTE_EXACT),
        }
        return RepresentationVerdict(
            site_id, url, key, 304, trigger.recorded_at, trigger.origin,
            trigger.etag, trigger.vary, EVIDENCE_WEAK_304,
            chosen.body_sha256, candidate["body_path"], False, contracts,
            None, chain, candidates,
        )
    if not etag_strong_match(trigger.etag, chosen.etag):
        return _blocked_304(site_id, url, key, trigger, VALIDATOR_MISMATCH,
                            chain, candidates)
    contracts = {
        "semantic-readable": _contract(True, None),
        "byte-exact": _contract(True, None),
    }
    return RepresentationVerdict(
        site_id, url, key, 304, trigger.recorded_at, trigger.origin,
        trigger.etag, trigger.vary, EVIDENCE_STRONG_304,
        chosen.body_sha256, candidate["body_path"], True, contracts,
        None, chain, candidates,
    )


def _blocked_304(
    site_id: str, url: str, key: str, trigger: RepresentationFact,
    reason: str, chain: list[dict[str, object]],
    candidates: list[dict[str, object]],
) -> RepresentationVerdict:
    return RepresentationVerdict(
        site_id, url, key, 304, trigger.recorded_at, trigger.origin,
        trigger.etag, trigger.vary, EVIDENCE_UNPUBLISHED, None, None, False,
        {"semantic-readable": _contract(False, reason),
         "byte-exact": _contract(False, reason)},
        reason, chain, candidates,
    )


def _contract(publishable: bool, reason: str | None) -> dict[str, object]:
    return {"publishable": publishable, "not_publishable_reason": reason}
