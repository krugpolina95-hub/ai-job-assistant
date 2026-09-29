"""The curated skill dictionary (GAP-14, architecture §7.3): `app/db/seed/skills.json` is the
source of truth, and `python -m app.cli skills sync` makes the database match it.

Sync is idempotent. It adds new skills, renames by normalized key and replaces the alias set.
It never deletes a skill: users may have it (`user_skills` restricts deletion).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Skill, SkillAlias
from app.repositories.profile import SkillRepository
from app.services.normalization import normalize_skill

logger = structlog.stdlib.get_logger(__name__)

DICTIONARY_PATH = Path(__file__).resolve().parents[1] / "db" / "seed" / "skills.json"


@dataclass(frozen=True)
class SkillEntry:
    name: str
    key: str
    aliases: dict[str, str | None] = field(default_factory=dict)  # alias key → locale


@dataclass(frozen=True)
class SyncResult:
    skills_created: int
    skills_total: int
    aliases_total: int


def load_dictionary(path: Path = DICTIONARY_PATH) -> list[SkillEntry]:
    """Parses and checks the dictionary: every name and alias must map to exactly one
    skill, or matching and profile input would be ambiguous."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries: list[SkillEntry] = []
    owner: dict[str, str] = {}

    def claim(key: str, name: str) -> None:
        if not key:
            raise ValueError(f"Empty name or alias for {name!r}")
        if owner.setdefault(key, name) != name:
            raise ValueError(f"{key!r} is used by both {owner[key]!r} and {name!r}")

    for item in raw:
        name = item["name"].strip()
        key = normalize_skill(name)
        claim(key, name)
        aliases: dict[str, str | None] = {}
        for locale, values in item.get("aliases", {}).items():
            for value in values:
                alias = normalize_skill(value)
                if alias == key or alias in aliases:
                    raise ValueError(f"Redundant alias {value!r} for {name!r}")
                claim(alias, name)
                aliases[alias] = locale
        entries.append(SkillEntry(name=name, key=key, aliases=aliases))
    return entries


class SkillDictionaryService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._skills = SkillRepository(db)

    async def sync(self, path: Path = DICTIONARY_PATH) -> SyncResult:
        entries = load_dictionary(path)
        existing_aliases = await self._skills.aliases()
        wanted_aliases: set[str] = set()
        created = 0
        for entry in entries:
            skill = await self._skills.get_by_normalized_name(entry.key)
            if skill is None:
                skill = self._skills.add(Skill(name=entry.name, normalized_name=entry.key))
                await self._skills.flush()
                created += 1
            skill.name = entry.name
            for alias_key, locale in entry.aliases.items():
                wanted_aliases.add(alias_key)
                alias = existing_aliases.get(alias_key)
                if alias is None:
                    self._db.add(
                        SkillAlias(alias_normalized=alias_key, skill_id=skill.id, locale=locale)
                    )
                else:
                    alias.skill_id = skill.id
                    alias.locale = locale
        for alias_key, alias in existing_aliases.items():
            if alias_key not in wanted_aliases:
                await self._db.delete(alias)
        await self._db.commit()
        alias_count = sum(len(entry.aliases) for entry in entries)
        logger.info("skills_synced", created=created, total=len(entries), aliases=alias_count)
        return SyncResult(
            skills_created=created, skills_total=len(entries), aliases_total=alias_count
        )
