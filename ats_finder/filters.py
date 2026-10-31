"""Deterministic Java and Canadian-location filters."""

from __future__ import annotations

import html
import re

from .models import LocationDecision, LocationMatch, WorkplaceType


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_JAVA_RE = re.compile(r"(?<!\w)java(?!\w)", re.IGNORECASE)
_EXCLUDED_TITLE_RE = re.compile(
    r"(?<!\w)(?:staff|principle|principal|manager|architect)(?!\w)",
    re.IGNORECASE,
)
_LEAD_TITLE_RE = re.compile(r"lead", re.IGNORECASE)

_OTTAWA_RE = re.compile(r"\b(?:ottawa|kanata|stittsville)\b", re.IGNORECASE)
_CANADA_REGION_PATTERN = (
    r"(?:canada|canadian|ontario|quebec|québec|british\s+columbia|"
    r"alberta|manitoba|saskatchewan|nova\s+scotia|new\s+brunswick|"
    r"newfoundland(?:\s+and\s+labrador)?|prince\s+edward\s+island)"
)
_CANADA_LOCATION_RE = re.compile(
    rf"\b{_CANADA_REGION_PATTERN}\b",
    re.IGNORECASE,
)
_CANADA_CITY_RE = re.compile(
    r"\b(?:"
    r"vancouver|calgary|edmonton|winnipeg|montreal|montréal|"
    r"halifax|quebec\s+city|ville\s+de\s+québec|"
    r"regina|saskatoon|st\.?\s+john'?s|fredericton|moncton|"
    r"charlottetown"
    r")\b",
    re.IGNORECASE,
)
_ONTARIO_CITY_RE = re.compile(
    r"\b(?:"
    r"ajax|brampton|etobicoke|guelph|kitchener|markham|mississauga|"
    r"north\s+york|oakville|oshawa|ottawa|pickering|richmond\s+hill|"
    r"scarborough|stittsville|sudbury|thunder\s+bay|toronto|vaughan|whitby"
    r")\b",
    re.IGNORECASE,
)
_REMOTE_RE = re.compile(
    r"\b(?:remote(?:ly)?|work(?:ing)?\s+from\s+home|home[- ]based|telecommut(?:e|ing))\b",
    re.IGNORECASE,
)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_ONSITE_RE = re.compile(r"\b(?:on[- ]?site|in[- ]office)\b", re.IGNORECASE)

_CANADA_EXCLUDED_RE = re.compile(
    r"\b(?:"
    r"(?:not|isn['’]?t|aren['’]?t)\s+(?:currently\s+)?(?:available|eligible|open)\s+(?:in|to|for)\s+canada"
    r"|cannot\s+(?:hire|employ|accept|work\s+with)\b.{0,30}\bcanad(?:a|ian)s?"
    r"|excluding\s+canada"
    r"|canada\s+(?:is\s+)?(?:excluded|ineligible|not\s+eligible)"
    r"|(?:candidates|applicants|workers)\s+(?:located\s+)?in\s+canada\s+(?:are\s+)?not\s+eligible"
    r")\b",
    re.IGNORECASE,
)

_CANADA_REMOTE_PATTERNS = (
    # Location-style adjacency: "Remote - Canada", "Remote (Canada)",
    # "Remote Ontario", "Remote from Canada". Incidental phrases such as
    # "remote) Working model: ... US & Canada-based team" do not match.
    re.compile(
        rf"\bremote\b\s*[-,–—(|/)]?\s*(?:in|from|within|across)?\s*"
        rf"(?:(?:the\s+)?(?:province|territory)\s+of\s+)?"
        rf"{_CANADA_REGION_PATTERN}\b",
        re.IGNORECASE,
    ),
    # Reverse location-style: "Canada (Remote)", "Canada - Remote".
    re.compile(
        rf"\b{_CANADA_REGION_PATTERN}\b\s*[-,–—(|/)]?\s*remote\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\banywhere\s+(?:within|in|across)\s+{_CANADA_REGION_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bwork(?:ing)?\s+from\s+(?:anywhere\s+(?:in|within)\s+)?"
        rf"{_CANADA_REGION_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcanada[- ]wide\b", re.IGNORECASE),
    # Eligibility language, not a mere team or customer mention.
    re.compile(
        r"\bopen\s+to\s+(?:applicants|candidates|anyone|people)\b"
        rf".{{0,80}}\b{_CANADA_REGION_PATTERN}\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"\b(?:hiring|recruiting)\b.{{0,40}}\b(?:in|across|within)\s+"
        rf"{_CANADA_REGION_PATTERN}\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:must|required)\s+(?:be\s+)?(?:reside|live|be\s+located)\s+in\s+"
        rf"(?:(?:the\s+)?(?:province|territory)\s+of\s+)?"
        rf"{_CANADA_REGION_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:authori[sz]ed|eligible)\s+to\s+work\s+in\s+"
        rf"(?:(?:the\s+)?(?:province|territory)\s+of\s+)?"
        rf"{_CANADA_REGION_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:open|available)\s+to\b.{{0,80}}\b"
        rf"{_CANADA_REGION_PATTERN}\b.{{0,30}}\b"
        r"(?:applicants|candidates|people|residents|workers)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)

_US_ONLY_RE = re.compile(
    r"\b(?:"
    r"(?:u\.?s\.?|united\s+states)(?:[- ]based)?\s+(?:only|remote\s+only)"
    r"|remote\s+(?:within|in|from)\s+(?:the\s+)?(?:u\.?s\.?|united\s+states)\s+only"
    r"|must\s+(?:be\s+)?(?:reside|live|be\s+located)\s+in\s+(?:the\s+)?(?:u\.?s\.?|united\s+states)"
    r")\b",
    re.IGNORECASE,
)
_EUROPE_UK_ONLY_RE = re.compile(
    r"\b(?:"
    r"(?:europe|european\s+union|eu|united\s+kingdom|u\.?k\.?)\s+(?:only|remote\s+only)"
    r"|remote\s+(?:within|in|from)\s+(?:europe|the\s+eu|the\s+united\s+kingdom|the\s+u\.?k\.?)"
    r")\b",
    re.IGNORECASE,
)
_WORLDWIDE_RE = re.compile(r"\b(?:worldwide|anywhere\s+in\s+the\s+world|global(?:ly)?)\b", re.IGNORECASE)
_NON_OTTAWA_EXPLICIT_RE = re.compile(
    r"\b(?:toronto|vancouver|montreal|calgary|edmonton|winnipeg|halifax)\b",
    re.IGNORECASE,
)
_NON_CANADA_COUNTRY_RE = re.compile(
    r"\b(?:"
    r"united\s+states|u\.?s\.?a?|usa|united\s+kingdom|u\.?k\.?|"
    r"ireland|india|australia|germany|france|spain|portugal|poland|"
    r"brazil|mexico|argentina|nigeria|kenya|south\s+africa"
    r")\b",
    re.IGNORECASE,
)


def plain_text(value: str | None) -> str:
    """Convert lightweight HTML content into normalized searchable text."""

    if not value:
        return ""
    return _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def matches_java(title: str | None, description: str | None) -> bool:
    """Match the standalone technology word ``Java`` in title or description.

    The word-boundary expression accepts punctuation such as ``Java-based`` and
    versions such as ``Java 17`` while rejecting ``JavaScript``.
    """

    return bool(_JAVA_RE.search(plain_text(title)) or _JAVA_RE.search(plain_text(description)))


def has_excluded_title(title: str | None) -> bool:
    """Exclude seniority/management words and any title containing "lead"."""

    title_text = plain_text(title)
    return bool(
        _EXCLUDED_TITLE_RE.search(title_text)
        or _LEAD_TITLE_RE.search(title_text)
    )


def infer_workplace_type(
    location: str | None,
    description: str | None = None,
) -> WorkplaceType:
    """Infer work mode conservatively when the ATS has no structured value."""

    location_text = plain_text(location)
    description_text = plain_text(description)
    for text in (location_text, description_text):
        if _HYBRID_RE.search(text):
            return WorkplaceType.HYBRID
        if _ONSITE_RE.search(text):
            return WorkplaceType.ONSITE
        if _REMOTE_RE.search(text):
            return WorkplaceType.REMOTE
    return WorkplaceType.UNKNOWN


def location_may_be_relevant(location: str | None) -> bool:
    """Return whether a summary location needs full content inspection.

    Missing, remote, or worldwide-only locations remain relevant because the
    description may establish Canadian eligibility. Clearly non-Canadian
    locations are excluded without downloading descriptions.
    """

    location_text = plain_text(location)
    if not location_text:
        return True
    if _OTTAWA_RE.search(location_text) or _CANADA_LOCATION_RE.search(location_text):
        return True
    if _ONTARIO_CITY_RE.search(location_text):
        return True
    if _NON_CANADA_COUNTRY_RE.search(location_text):
        return False
    if _CANADA_CITY_RE.search(location_text):
        return True
    return bool(_REMOTE_RE.search(location_text) or _WORLDWIDE_RE.search(location_text))


def classify_location(
    location: str | None,
    description: str | None = None,
    workplace_type: WorkplaceType = WorkplaceType.UNKNOWN,
    canada_remote_eligibility: bool | None = None,
) -> LocationDecision:
    """Classify Ottawa or explicitly Canada-remote eligibility.

    Ambiguous remote roles are deliberately routed to manual review rather than
    admitted to confirmed results. Structured provider evidence is honored, but
    a structured Canada-remote value does not turn a non-remote job into one.
    """

    supplied_mode = WorkplaceType(workplace_type)
    location_text = plain_text(location)
    description_text = plain_text(description)
    combined = f"{location_text} {description_text}".strip()
    mode = supplied_mode
    if mode is WorkplaceType.UNKNOWN:
        mode = infer_workplace_type(location_text, description_text)

    if _CANADA_EXCLUDED_RE.search(combined):
        return LocationDecision(
            LocationMatch.REJECTED,
            mode,
            False,
            "canada_explicitly_excluded",
        )

    if _OTTAWA_RE.search(location_text):
        return LocationDecision(
            LocationMatch.CONFIRMED,
            mode,
            canada_remote_eligibility,
            "ottawa_area_location",
        )

    # Hybrid and on-site jobs qualify only in the Ottawa area. A generic
    # remote-work sentence elsewhere in a Toronto/Vancouver/etc. description
    # must not override the posting's structured or explicit work mode.
    if mode in (WorkplaceType.HYBRID, WorkplaceType.ONSITE):
        return LocationDecision(
            LocationMatch.REJECTED,
            mode,
            canada_remote_eligibility,
            "non_ottawa_hybrid_or_onsite",
        )

    explicit_canada_remote = any(pattern.search(combined) for pattern in _CANADA_REMOTE_PATTERNS)
    remote_context = mode is WorkplaceType.REMOTE or bool(_REMOTE_RE.search(combined))
    canada_location = _CANADA_LOCATION_RE.search(location_text)
    ontario_city = _ONTARIO_CITY_RE.search(location_text)
    canada_city = _CANADA_CITY_RE.search(location_text)
    foreign_country = _NON_CANADA_COUNTRY_RE.search(location_text)
    location_evidence = bool(
        canada_location
        or ontario_city
        or (canada_city and not foreign_country)
    )

    if (
        canada_remote_eligibility is False
        and remote_context
        and not location_evidence
        and not explicit_canada_remote
    ):
        return LocationDecision(
            LocationMatch.REJECTED,
            mode,
            False,
            "structured_canada_remote_ineligible",
        )

    # Explicit region-only language takes precedence over incidental mentions
    # of Canada elsewhere in a description.
    if remote_context and _US_ONLY_RE.search(combined):
        return LocationDecision(
            LocationMatch.REJECTED,
            mode,
            False,
            "us_only_remote",
        )
    if remote_context and _EUROPE_UK_ONLY_RE.search(combined):
        return LocationDecision(
            LocationMatch.REJECTED,
            mode,
            False,
            "europe_or_uk_only_remote",
        )

    # A remote role with Canada/Ontario or an Ontario city in its structured
    # location field qualifies even when remote work was established through the
    # description or structured workplace type rather than the location text.
    if mode is WorkplaceType.REMOTE and location_evidence:
        return LocationDecision(
            LocationMatch.CONFIRMED,
            WorkplaceType.REMOTE,
            True,
            (
                "canada_remote_explicit"
                if canada_location
                else "ontario_remote_explicit" if ontario_city else "canada_remote_city"
            ),
        )

    if explicit_canada_remote and remote_context:
        return LocationDecision(
            LocationMatch.CONFIRMED,
            WorkplaceType.REMOTE if mode is WorkplaceType.UNKNOWN else mode,
            True,
            "canada_remote_explicit",
        )

    if canada_remote_eligibility is True and mode is WorkplaceType.REMOTE:
        return LocationDecision(
            LocationMatch.CONFIRMED,
            mode,
            True,
            "structured_canada_remote_eligible",
        )

    if remote_context:
        reason = "worldwide_remote_without_explicit_canada" if _WORLDWIDE_RE.search(combined) else "remote_eligibility_ambiguous"
        return LocationDecision(
            LocationMatch.REVIEW_LOCATION,
            WorkplaceType.REMOTE if mode is WorkplaceType.UNKNOWN else mode,
            canada_remote_eligibility,
            reason,
        )

    if _NON_OTTAWA_EXPLICIT_RE.search(location_text):
        reason = "non_ottawa_location"
    else:
        reason = "no_ottawa_or_canada_remote_evidence"
    return LocationDecision(
        LocationMatch.REJECTED,
        mode,
        canada_remote_eligibility,
        reason,
    )

