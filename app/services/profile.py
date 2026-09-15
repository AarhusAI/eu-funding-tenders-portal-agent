"""Search-profile semantics: build the server-side query, then filter and rank.

This module is **deliberately pure** — no network, no LLM, no clock. It takes
trimmed topic dicts (as produced by ``app.services.corpus``) plus a
``SearchProfile`` and returns filtered, scored topics. That purity is what makes
the README's 100% coverage target realistic, and it is the reason the ranking
rules can be tested exhaustively.

Why filtering is split between server and client: the Portal's query DSL accepts
only ``bool``/``must``/``terms``. ``wildcard`` and ``match`` are rejected with
``400 businessError: "Invalid Query format"``, so identifier substring matching
(``topic_contains``, ``clusters``) *cannot* be pushed server-side and has to run
here over the whole retrieved corpus.
"""

import re

from app.config import settings
from app.models import SearchProfile

# --- Verified Portal filter values (see "The Portal API" in README.md) --------

TYPE_CALL_TOPIC = "1"

# These are the Portal's own taxonomy ids — the literal values its frontend
# posts — verified against production. They are stable: a programme's id does
# not change once published. What *does* happen is that the EU publishes a new
# programme, so PORTAL_PROGRAMME_IDS / PORTAL_STATUS_IDS (JSON objects of
# name -> id) merge over the built-ins below. Adding one is then an env var
# rather than a release. To find an id: open the Portal's search page, filter by
# the programme, and read the `frameworkProgramme` terms value off the
# search-api request in the browser's network tab.
_BUILTIN_PROGRAMME_IDS = {
    "horizon europe": "43108390",
    "digital europe": "43152860",
    "eu4health": "43332642",
}
_BUILTIN_STATUS_IDS = {
    "forthcoming": "31094501",
    "open for submission": "31094502",
    "closed": "31094503",
}


def _with_overrides(builtin: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    """Merge env overrides over the built-in table, lower-casing their keys.

    Lookup is case-insensitive (see ``_lookup``), so an override keyed
    "Horizon Europe" has to be normalised or it would add a second entry
    instead of replacing the built-in one.
    """
    return {**builtin, **{name.strip().lower(): id_ for name, id_ in overrides.items()}}


PROGRAMME_IDS = _with_overrides(_BUILTIN_PROGRAMME_IDS, settings.portal_programme_ids)
STATUS_IDS = _with_overrides(_BUILTIN_STATUS_IDS, settings.portal_status_ids)

# Reverse maps, derived *after* the merge so an overridden id still resolves to
# a readable name in the response.
PROGRAMME_NAMES = {v: k.title() for k, v in PROGRAMME_IDS.items()}
STATUS_NAMES = {v: k.capitalize() for k, v in STATUS_IDS.items()}

# The keyword list from README.md. Duplicates in the README ("Citizen
# Engagement" appears twice) are harmless — _score() dedupes case-insensitively.
DEFAULT_KEYWORDS = [
    "IoT",
    "Data infrastructure",
    "Digital platform",
    "Open source",
    "Cloud",
    "Edge",
    "Digital Twin",
    "Generative AI",
    "GenAI",
    "Automated Systems",
    "Web 4.0",
    "NEB",
    "GovTech",
    "New European Bauhaus",
    "Citizen Engagement",
    "Local Pilot",
    "Real world testing",
    "Test and Experimentation",
    "Dataspace",
    "Societal Readiness",
    "TRL 4-7",
    "SRL 3-8",
    "Interoperability",
    "Smart Energy",
    "Smart Building",
    "Smart Grid",
    "Mobility",
    "Governance",
    "Standardization",
    "Robotics",
    "Digital transformation",
    "Smart city",
    "Stakeholder engagement",
    "Remote sensing",
    "Climate",
    "Health",
    "Democracy",
    "Resilience",
    "Research",
    "Innovation",
    "Policy",
    "Local government",
    "Local authority",
]

# The README's example profile, used whenever a request omits a field.
DEFAULT_PROFILE = SearchProfile(
    programmes=["Horizon Europe", "Digital Europe"],
    statuses=["Forthcoming", "Open for submission"],
    topic_contains=["MISS"],
    clusters=["CL2", "CL4", "CL5"],
    keywords=DEFAULT_KEYWORDS,
)

# A metadata hit is worth more than a description-only hit: across the 970-topic
# English corpus "Cloud" appears in 26 topics' metadata but 44 more only in the
# description, so weighting them equally would let incidental prose outrank a
# topic that is genuinely *about* the keyword.
_SCORE_METADATA = 2
_SCORE_DESCRIPTION = 1


def resolve(profile: SearchProfile | None) -> SearchProfile:
    """Layer a caller's partial profile over the README default."""
    if profile is None:
        return DEFAULT_PROFILE
    return profile.merged_over(DEFAULT_PROFILE)


def resolve_languages(profile: SearchProfile) -> list[str]:
    """Which language copies to retrieve — profile first, then the env default."""
    return list(profile.languages or settings.portal_languages)


def _lookup(
    names: list[str] | None, table: dict[str, str], label: str
) -> tuple[list[str], list[str]]:
    """Map human-readable names to Portal ids, collecting unknown ones as warnings.

    An unrecognised name is *reported*, never silently dropped: quietly ignoring
    a typo'd programme would widen the search while looking like it had narrowed.
    """
    ids: list[str] = []
    warnings: list[str] = []
    for name in names or []:
        key = name.strip().lower()
        if key in table:
            ids.append(table[key])
        else:
            warnings.append(f"unknown {label}: {name!r} (known: {', '.join(sorted(table))})")
    return ids, warnings


def server_query(profile: SearchProfile) -> tuple[dict, list[str]]:
    """Build the Portal ``query`` body for a profile, plus any name warnings.

    Only the filters the Portal actually supports go in here: document type,
    submission status and framework programme. Everything else is client-side.
    """
    programme_ids, programme_warnings = _lookup(profile.programmes, PROGRAMME_IDS, "programme")
    status_ids, status_warnings = _lookup(profile.statuses, STATUS_IDS, "status")

    must: list[dict] = [{"terms": {"type": [TYPE_CALL_TOPIC]}}]
    if status_ids:
        must.append({"terms": {"status": status_ids}})
    if programme_ids:
        must.append({"terms": {"frameworkProgramme": programme_ids}})
    return {"bool": {"must": must}}, programme_warnings + status_warnings


def identifier_query(identifier: str) -> dict:
    """Build the query for one exact topic identifier.

    ``type`` is pinned to call-topic: several unrelated documents can share an
    identifier, and an unconstrained lookup returned 5 rows for one topic — the
    first of which was a stub with no status, deadline or description.
    """
    return {
        "bool": {
            "must": [
                {"terms": {"type": [TYPE_CALL_TOPIC]}},
                {"terms": {"identifier": [identifier]}},
            ]
        }
    }


# --- Client-side filtering and ranking ---------------------------------------


def _dedupe_keywords(keywords: list[str] | None) -> list[str]:
    """Drop case-insensitive duplicates, keeping first appearance and original case."""
    seen: set[str] = set()
    out: list[str] = []
    for keyword in keywords or []:
        key = keyword.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(keyword.strip())
    return out


def _pattern(keyword: str) -> re.Pattern[str]:
    r"""Compile a word-boundary, case-insensitive matcher for one keyword.

    ``re.escape`` keeps punctuation literal so "Web 4.0" and "TRL 4-7" match
    themselves rather than acting as regex. The ``\b`` guards stop "Cloud" from
    matching "clouded" — without them the score inflates on unrelated prose.
    A trailing non-word character (the "7" in "TRL 4-7" is a word char, but a
    keyword could end in punctuation) would make a trailing ``\b`` never match,
    so each boundary is only applied when the adjacent character is a word one.
    """
    lead = r"\b" if keyword[:1].isalnum() or keyword[:1] == "_" else ""
    trail = r"\b" if keyword[-1:].isalnum() or keyword[-1:] == "_" else ""
    return re.compile(lead + re.escape(keyword) + trail, re.IGNORECASE)


def _haystacks(topic: dict) -> tuple[str, str]:
    """Return the (metadata, description) text a keyword may match in."""
    parts: list[str] = [str(topic.get("title") or "")]
    parts += [str(k) for k in topic.get("keywords") or []]
    parts += [str(t) for t in topic.get("tags") or []]
    return " ".join(parts), str(topic.get("description") or "")


def score(topic: dict, keywords: list[str] | None) -> tuple[int, list[str]]:
    """Score one topic additively over the *distinct* keywords it matches.

    A keyword found in the title/keywords/tags scores ``_SCORE_METADATA``; one
    found only in the description scores ``_SCORE_DESCRIPTION``. A keyword that
    matches in both places is counted once, at the higher weight.
    """
    metadata_text, description_text = _haystacks(topic)
    total = 0
    matched: list[str] = []
    for keyword in _dedupe_keywords(keywords):
        pattern = _pattern(keyword)
        if pattern.search(metadata_text):
            total += _SCORE_METADATA
            matched.append(keyword)
        elif pattern.search(description_text):
            total += _SCORE_DESCRIPTION
            matched.append(keyword)
    return total, matched


def _matches_identifier_filters(identifier: str, profile: SearchProfile) -> bool:
    """Apply the ``topic_contains`` / ``clusters`` filters to one identifier.

    The two are **OR**-ed, per the README: the example profile wants topics with
    "MISS" in the identifier *or* in cluster 2, 4 or 5 — reading it as AND would
    return almost nothing. When neither filter is set, every topic passes.
    """
    needles = [n for n in (profile.topic_contains or []) if n]
    clusters = [c for c in (profile.clusters or []) if c]
    if not needles and not clusters:
        return True
    upper = identifier.upper()
    if any(needle.upper() in upper for needle in needles):
        return True
    return any(cluster.upper() in upper for cluster in clusters)


def _deadline_sort_key(topic: dict) -> str:
    """Sort key for the deadline: soonest first, topics without one last."""
    # "~" sorts after every digit, so a missing deadline lands at the end.
    return str(topic.get("deadline_date") or "~")


def apply(topics: list[dict], profile: SearchProfile) -> list[dict]:
    """Filter ``topics`` by the profile's identifier rules, then rank them.

    Returns new dicts carrying ``score`` and ``matched_keywords``; the input is
    not mutated. Ordering is score descending, then soonest deadline, then
    identifier — the last making the order stable and reproducible in tests.
    """
    scored: list[dict] = []
    for topic in topics:
        identifier = str(topic.get("identifier") or "")
        if not _matches_identifier_filters(identifier, profile):
            continue
        total, matched = score(topic, profile.keywords)
        scored.append({**topic, "score": total, "matched_keywords": matched})

    scored.sort(key=lambda t: (-t["score"], _deadline_sort_key(t), str(t.get("identifier") or "")))
    return scored
