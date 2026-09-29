"""Operator commands (GAP-01). Run from backend/:

uv run python -m app.cli sources sync
uv run python -m app.cli sources list
uv run python -m app.cli sources enable adzuna --confirm-compliance-review
uv run python -m app.cli sources disable adzuna
uv run python -m app.cli sources run adzuna --country GB --keywords python --days 7
"""

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.db.session import create_db_engine, create_session_factory
from app.integrations.sources.rate_limit import RedisSourceRateLimiter
from app.services.source_admin_service import SourceAdminService
from app.services.source_run_service import SourceRunService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    groups = parser.add_subparsers(dest="group", required=True)
    sources = groups.add_parser("sources", help="job sources").add_subparsers(
        dest="command", required=True
    )
    sources.add_parser("sync", help="register or refresh all sources defined in code")
    sources.add_parser("list", help="show sources and their status")
    enable = sources.add_parser("enable", help="allow a source to run")
    enable.add_argument("key")
    enable.add_argument(
        "--confirm-compliance-review",
        action="store_true",
        help="record that the SRS §17 review in docs/source-compliance.md is complete",
    )
    sources.add_parser("disable", help="stop a source from running").add_argument("key")
    run = sources.add_parser("run", help="fetch and import vacancies from one source now")
    run.add_argument("key")
    run.add_argument("--country", required=True, help="ISO 3166-1 alpha-2, e.g. GB")
    run.add_argument("--keywords", nargs="+", default=[])
    run.add_argument("--city")
    run.add_argument("--days", type=int, dest="published_within_days")
    return parser


async def execute(args: argparse.Namespace, db: AsyncSession, settings: Settings) -> Any:
    admin = SourceAdminService(db)
    match args.command:
        case "sync":
            return {"created": await admin.sync_definitions()}
        case "list":
            return [
                {
                    "key": s.adapter_key,
                    "name": s.name,
                    "enabled": s.is_enabled,
                    "compliance_reviewed_at": s.compliance_reviewed_at,
                    "storage_policy": s.storage_policy,
                }
                for s in await admin.list_sources()
            ]
        case "enable":
            source = await admin.enable(
                args.key, compliance_review_confirmed=args.confirm_compliance_review
            )
            return {"key": source.adapter_key, "enabled": source.is_enabled}
        case "disable":
            source = await admin.disable(args.key)
            return {"key": source.adapter_key, "enabled": source.is_enabled}
        case "run":
            params = {
                "country": args.country,
                "keywords": args.keywords,
                "city": args.city,
                "published_within_days": args.published_within_days,
            }
            redis = create_redis(settings)
            try:
                limiter = RedisSourceRateLimiter(redis)
                result = await SourceRunService(db, settings, limiter).run(args.key, params)
            finally:
                await redis.aclose()
            return dataclasses.asdict(result) | {"vacancy_ids": len(result.vacancy_ids)}
    raise AssertionError(f"unhandled command {args.command}")


async def _main(argv: Sequence[str]) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    # Logs go to stderr so stdout carries only the JSON result (scriptable).
    configure_logging(settings.log_level, "console", stream=sys.stderr)
    engine = create_db_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            output = await execute(args, db, settings)
    except AppError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}), file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(json.dumps(output, indent=2, default=str))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
