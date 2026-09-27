"""All ORM models. Importing this package registers every table on `Base.metadata`."""

from app.models.ai_interaction import AIInteraction
from app.models.history import SearchHistoryEvent
from app.models.job import SearchJob, SearchJobResult, SearchJobSource
from app.models.match import VacancyMatch
from app.models.profile import UserProfile
from app.models.saved import SavedVacancy
from app.models.search import Search
from app.models.skill import Skill, SkillAlias, UserSkill
from app.models.source import Source
from app.models.user import User
from app.models.vacancy import Vacancy

__all__ = [
    "AIInteraction",
    "SavedVacancy",
    "Search",
    "SearchHistoryEvent",
    "SearchJob",
    "SearchJobResult",
    "SearchJobSource",
    "Skill",
    "SkillAlias",
    "Source",
    "User",
    "UserProfile",
    "UserSkill",
    "Vacancy",
    "VacancyMatch",
]
