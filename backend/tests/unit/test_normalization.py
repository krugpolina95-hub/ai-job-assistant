from decimal import Decimal

import pytest

from app.core.enums import SalaryPeriod, StoragePolicy
from app.services.normalization import (
    SNIPPET_LENGTH,
    apply_storage_policy,
    canonical_url,
    content_hash,
    html_to_text,
    normalize_company,
    normalize_title,
    snippet,
    to_monthly_eur,
)

EUR_ONLY = {"EUR": Decimal(1)}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Jobs.Example.COM/Job/42/", "https://jobs.example.com/Job/42"),
        ("https://jobs.example.com:443/42", "https://jobs.example.com/42"),
        ("http://jobs.example.com:8080/42", "http://jobs.example.com:8080/42"),
        ("https://x.io/j?utm_source=li&id=7&gclid=abc&ref=home#apply", "https://x.io/j?id=7"),
        ("https://x.io/j?b=2&a=1", "https://x.io/j?a=1&b=2"),
        ("https://x.io/", "https://x.io"),
    ],
)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


def test_title_normalization_ignores_case_accents_and_punctuation() -> None:
    assert normalize_title("  Senior  Python-Developer (Remote)! ") == (
        "senior python developer remote"
    )
    assert normalize_title("Développeur Python") == normalize_title("developpeur python")
    assert normalize_title("Разработчик Python") == "разработчик python"


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Acme Ltd.", "acme"),
        ("ACME, Inc", "acme"),
        ("UAB Vilnius Tech", "vilnius tech"),
        ("Ltd", None),
        (None, None),
        ("", None),
    ],
)
def test_company_normalization(company: str | None, expected: str | None) -> None:
    assert normalize_company(company) == expected


def test_html_to_text_drops_markup_scripts_and_decodes_entities() -> None:
    markup = (
        "<h2>About</h2><p>We&nbsp;build <b>great</b> tools &amp; more.</p>"
        "<script>alert('x')</script><style>p{}</style>"
        "<ul><li>Python</li><li>SQL</li></ul><img src=x onerror=alert(1)>"
    )

    text = html_to_text(markup)

    assert text == "About\nWe build great tools & more.\nPython\nSQL"
    assert "alert" not in text
    assert "<" not in text


def test_content_hash_is_stable_across_formatting_and_changes_with_content() -> None:
    base = {
        "title": "Python Developer",
        "company": "Acme Ltd",
        "country": "LT",
        "city": "Vilnius",
        "remote_type": "hybrid",
        "description": "Build APIs.",
    }
    reformatted = base | {"title": "  python  DEVELOPER ", "company": "ACME", "city": "VILNIUS"}

    assert content_hash(**base) == content_hash(**reformatted)
    assert len(content_hash(**base)) == 64
    assert content_hash(**base) != content_hash(**base | {"city": "Kaunas"})
    assert content_hash(**base) != content_hash(**base | {"description": "Build UIs."})


def test_snippet_is_capped() -> None:
    assert snippet(None) is None
    assert snippet("short") == "short"
    long = snippet("x" * 2000)
    assert long is not None
    assert len(long) == SNIPPET_LENGTH
    assert long.endswith("…")


@pytest.mark.parametrize(
    ("policy", "expected_description", "has_snippet"),
    [
        (StoragePolicy.FULL, "Full text", True),
        (StoragePolicy.SNIPPET, None, True),
        (StoragePolicy.NONE, None, False),
    ],
)
def test_storage_policy(
    policy: StoragePolicy, expected_description: str | None, has_snippet: bool
) -> None:
    description, short = apply_storage_policy("Full text", policy)

    assert description == expected_description
    assert (short is not None) is has_snippet


@pytest.mark.parametrize(
    ("amount", "currency", "period", "expected"),
    [
        (Decimal(3000), "EUR", SalaryPeriod.MONTH, Decimal("3000.00")),
        (Decimal(36000), "EUR", SalaryPeriod.YEAR, Decimal("3000.00")),
        (Decimal(20), "EUR", SalaryPeriod.HOUR, Decimal("3360.00")),
        (Decimal(100), "EUR", SalaryPeriod.DAY, Decimal("2170.00")),
        (Decimal(3000), "EUR", SalaryPeriod.UNKNOWN, None),
        (Decimal(3000), "EUR", None, None),
        (Decimal(3000), "USD", SalaryPeriod.MONTH, None),
        (None, "EUR", SalaryPeriod.MONTH, None),
    ],
)
def test_monthly_eur_conversion(
    amount: Decimal | None, currency: str, period: SalaryPeriod | None, expected: Decimal | None
) -> None:
    assert to_monthly_eur(amount, currency, period, EUR_ONLY) == expected


def test_monthly_eur_uses_supplied_rates() -> None:
    assert to_monthly_eur(Decimal(1000), "USD", SalaryPeriod.MONTH, {"USD": Decimal("0.9")}) == (
        Decimal("900.00")
    )
