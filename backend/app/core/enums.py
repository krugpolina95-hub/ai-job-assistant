"""Domain vocabularies shared by models, schemas and the generated frontend types.

Each enum is stored as Postgres `text` guarded by a CHECK constraint (see app/db/types.py).
"""

from enum import StrEnum


class SupportedLocale(StrEnum):
    """UI languages (TD-20). Adding one also needs a frontend catalog and email templates."""

    EN = "en"
    RU = "ru"


class WorkFormat(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class ExperienceLevel(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"


class SalaryPeriod(StrEnum):
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class SkillLevel(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class LocationSource(StrEnum):
    MANUAL = "manual"
    IP = "ip"
    BROWSER = "browser"


class SourceType(StrEnum):
    API = "api"
    RSS = "rss"
    ATS = "ats"


class StoragePolicy(StrEnum):
    FULL = "full"
    SNIPPET = "snippet"
    NONE = "none"


class SearchChannel(StrEnum):
    FORM = "form"
    AI = "ai"


class SearchStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    ARCHIVED = "archived"


class SearchJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_SEARCH_JOB_STATUSES = (SearchJobStatus.PENDING, SearchJobStatus.RUNNING)


class SearchJobSourceStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


class MatchLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExplanationSource(StrEnum):
    TEMPLATE = "template"
    AI = "ai"


class HistoryEventType(StrEnum):
    SEARCH_CREATED = "search_created"
    SEARCH_CONFIRMED = "search_confirmed"
    SEARCH_RUN = "search_run"


class AITask(StrEnum):
    PARSE_QUERY = "parse_query"
    CLARIFY = "clarify"
    EXPLAIN_MATCH = "explain_match"


class AIInteractionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
