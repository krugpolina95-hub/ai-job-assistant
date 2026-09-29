"""Pure normalization rules for ingested vacancies (§3.7, TD-14, TD-16, TD-23, AMB-24).

No I/O here: every function is deterministic, so dedup keys are stable and unit-testable.
Adapters deliver structured fields; free-text parsing (salary, location, skills) comes later.
"""

import hashlib
import html
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.enums import SalaryPeriod, StoragePolicy

SNIPPET_LENGTH = 500
HASHED_DESCRIPTION_LENGTH = 500

# Query parameters that only track the click, never identify the posting.
_TRACKING_PARAMS = frozenset(
    {"gclid", "fbclid", "msclkid", "yclid", "mc_cid", "mc_eid", "ref", "referrer", "source"}
)
_TRACKING_PREFIXES = ("utm_",)

# Legal-form suffixes removed so "Acme Ltd." and "ACME" dedup as the same company.
_COMPANY_SUFFIXES = frozenset(
    {
        "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "ag",
        "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "oy", "ab", "as", "aps", "uab", "ou",
        "sp", "zoo", "plc", "pte", "pty", "kg", "ooo", "oao", "zao", "ip",
    }
)  # fmt: skip

# TD-14: fixed conversions to a monthly amount.
_MONTHLY_FACTOR = {
    SalaryPeriod.HOUR: Decimal(168),
    SalaryPeriod.DAY: Decimal("21.7"),
    SalaryPeriod.MONTH: Decimal(1),
    SalaryPeriod.YEAR: Decimal(1) / Decimal(12),
}

_WHITESPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _fold(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces: the comparison form."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return collapse_whitespace(_NON_WORD.sub(" ", without_marks).replace("_", " "))


def normalize_title(title: str) -> str:
    return _fold(title)


def normalize_skill(name: str) -> str:
    """Dictionary key of a skill name or alias (GAP-14): case- and spacing-insensitive, but
    punctuation is kept, because it tells skills apart ("C", "C++", "C#", ".NET")."""
    folded = collapse_whitespace(unicodedata.normalize("NFKC", name)).casefold()
    # Russian YO (U+0451) is commonly written as IE (U+0435).
    return folded.replace(chr(0x0451), chr(0x0435))


def normalize_company(company: str | None) -> str | None:
    if not company:
        return None
    words = [word for word in _fold(company).split() if word not in _COMPANY_SUFFIXES]
    return " ".join(words) or None


class _TextExtractor(HTMLParser):
    _SKIPPED = frozenset({"script", "style", "noscript", "template", "iframe", "svg"})
    _BLOCKS = frozenset(
        {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skip_depth += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Source HTML → plain text (TD-23). Tags, scripts and styles are dropped; nothing from the
    source is ever rendered as HTML (CLAUDE.md §6)."""
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    text = html.unescape("".join(extractor.parts))
    lines = (collapse_whitespace(line) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def canonical_url(url: str) -> str:
    """Stable form of a posting URL for dedup (TD-16): lowercase scheme and host, no default
    port, no fragment, no tracking parameters, sorted query, no trailing slash."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def location_key(country: str | None, city: str | None, remote_type: str | None) -> str:
    return "|".join((country or "", _fold(city) if city else "", remote_type or ""))


def content_hash(
    *,
    title: str,
    company: str | None,
    country: str | None,
    city: str | None,
    remote_type: str | None,
    description: str | None,
) -> str:
    """sha256 of the normalized AMB-24 fields: formatting differences don't change it."""
    parts = (
        normalize_title(title),
        normalize_company(company) or "",
        location_key(country, city, remote_type),
        _fold(description or "")[:HASHED_DESCRIPTION_LENGTH],
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def snippet(text: str | None) -> str | None:
    if not text:
        return None
    if len(text) <= SNIPPET_LENGTH:
        return text
    return text[: SNIPPET_LENGTH - 1].rstrip() + "…"


def apply_storage_policy(
    description: str | None, policy: StoragePolicy
) -> tuple[str | None, str | None]:
    """(description, description_snippet) allowed by the source's storage policy (TD-23)."""
    if policy is StoragePolicy.FULL:
        return description, snippet(description)
    if policy is StoragePolicy.SNIPPET:
        return None, snippet(description)
    return None, None


def to_monthly(amount: Decimal | None, period: SalaryPeriod | None) -> Decimal | None:
    """Monthly amount in the same currency (TD-14 factors); None if the period is unknown."""
    if amount is None or period not in _MONTHLY_FACTOR:
        return None
    return amount * _MONTHLY_FACTOR[period]


def to_monthly_eur(
    amount: Decimal | None,
    currency: str | None,
    period: SalaryPeriod | None,
    eur_rates: dict[str, Decimal],
) -> Decimal | None:
    """Approximate monthly EUR amount for filtering and sorting only (TD-14). Unknown period
    or currency without a rate → None: the value is never guessed."""
    monthly = to_monthly(amount, period)
    rate = eur_rates.get(currency) if currency else None
    if monthly is None or rate is None:
        return None
    return (monthly * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
