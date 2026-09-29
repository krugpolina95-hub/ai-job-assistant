from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.profile import (
    ProfileUpdate,
    SkillSearchParams,
    UserSkillIn,
    UserSkillsIn,
)
from app.services.normalization import normalize_skill
from app.services.skill_dictionary_service import load_dictionary


def test_partial_update_remembers_which_fields_were_sent() -> None:
    update = ProfileUpdate(summary=None, relocation_ready=True)

    assert update.model_fields_set == {"summary", "relocation_ready"}


def test_values_are_normalized() -> None:
    update = ProfileUpdate(
        desired_roles=["Python Developer", " python developer ", "Data Engineer"],
        location={"country": "lt", "city": "Vilnius"},
        salary={"min": 3000, "currency": "eur"},
        languages=[{"code": "EN", "level": "C1"}, {"code": "ru", "level": "native"}],
        work_formats=["remote", "remote", "hybrid"],
    )

    assert update.desired_roles == ["Python Developer", "Data Engineer"]
    assert update.location is not None and update.location.country == "LT"
    assert update.salary is not None
    assert (update.salary.currency, update.salary.period) == ("EUR", "month")
    assert [language.code for language in update.languages] == ["en", "ru"]
    assert update.work_formats == ["remote", "hybrid"]


@pytest.mark.parametrize(
    "body",
    [
        {"location": {}},
        {"location": {"country": "LTU"}},
        {"salary": {"min": 5000, "max": 3000, "currency": "EUR"}},
        {"salary": {"min": -1, "currency": "EUR"}},
        {"salary": {"min": 1000, "currency": "euro"}},
        {"salary": {"currency": "EUR"}},
        {"desired_roles": ["x"] * 11},
        {"desired_roles": [""]},
        {"languages": [{"code": "en", "level": "C1"}, {"code": "EN", "level": "B2"}]},
        {"languages": [{"code": "eng", "level": "C1"}]},
        {"languages": [{"code": "en", "level": "fluent"}]},
        {"experience_level": "guru"},
        {"work_formats": ["office"]},
        {"summary": "x" * 5001},
        {"display_name": "x" * 101},
        {"relocation_ready": None},
        {"desired_roles": None},
    ],
)
def test_invalid_profile_input_is_rejected(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProfileUpdate(**body)


@pytest.mark.parametrize(
    "field",
    ["age", "birth_date", "gender", "nationality", "religion", "health", "marital_status", "photo"],
)
def test_sensitive_attributes_are_never_accepted(field: str) -> None:
    with pytest.raises(ValidationError) as error:
        ProfileUpdate(**{field: "anything"})

    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    "item",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"name": "Python", "level": "wizard"},
        {"name": "Python", "years_experience": -1},
        {"name": "Python", "years_experience": 71},
        {"name": "Python", "years_experience": "2.25"},
    ],
)
def test_invalid_skill_rows_are_rejected(item: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UserSkillIn(**item)


def test_skill_list_is_bounded() -> None:
    assert UserSkillIn(name="Go", years_experience="2.5").years_experience == Decimal("2.5")
    with pytest.raises(ValidationError):
        UserSkillsIn(skills=[{"name": f"s{n}"} for n in range(101)])
    with pytest.raises(ValidationError):
        SkillSearchParams(q="")


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("  Python ", "python"),
        ("C++", "c++"),
        ("C#", "c#"),
        (".NET  Core", ".net core"),
        ("Бухгалтерский   Учёт", "бухгалтерский учет"),
        ("".join(chr(ord(c) + 0xFEE0) for c in "PYTHON"), "python"),  # full-width
    ],
)
def test_normalize_skill_keeps_meaningful_punctuation(raw: str, key: str) -> None:
    assert normalize_skill(raw) == key


def test_shipped_dictionary_is_consistent() -> None:
    entries = load_dictionary()

    keys = [entry.key for entry in entries]
    assert len(keys) == len(set(keys)) >= 150
    assert {"python", "c", "c++", "c#"} <= set(keys)
    aliases = {alias: entry.name for entry in entries for alias in entry.aliases}
    assert aliases["питон"] == "Python"
    assert aliases["управление проектами"] == "Project Management"
    assert not set(aliases) & set(keys)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('[{"name": "Go"}, {"name": " go "}]', "used by both"),
        ('[{"name": "Go", "aliases": {"en": ["golang"]}}, {"name": "Golang"}]', "used by both"),
        ('[{"name": "Go", "aliases": {"en": ["GO"]}}]', "Redundant"),
        ('[{"name": "Go", "aliases": {"en": ["  "]}}]', "Empty"),
    ],
)
def test_dictionary_conflicts_are_refused(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "skills.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_dictionary(path)
