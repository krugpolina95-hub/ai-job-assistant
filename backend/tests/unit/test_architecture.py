"""Layering rules (CLAUDE.md §6, architecture §4.1), checked on imports.

api → services → repositories → models
integrations are called only from services; core/db/schemas/models never import upward.
"""

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"

WEB_FRAMEWORKS = ("fastapi", "starlette")

# package → module prefixes it must not import
FORBIDDEN_IMPORTS: dict[str, tuple[str, ...]] = {
    "core": (*WEB_FRAMEWORKS, "app.api", "app.services", "app.repositories", "app.models"),
    "db": (*WEB_FRAMEWORKS, "app.api", "app.services", "app.repositories"),
    "models": (*WEB_FRAMEWORKS, "app.api", "app.services", "app.repositories", "app.schemas"),
    "schemas": (*WEB_FRAMEWORKS, "app.api", "app.services", "app.repositories", "app.models"),
    "repositories": (*WEB_FRAMEWORKS, "app.api", "app.services", "app.integrations"),
    "services": (*WEB_FRAMEWORKS, "app.api"),
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


CASES = [
    (package, path)
    for package in FORBIDDEN_IMPORTS
    for path in sorted((APP_DIR / package).rglob("*.py"))
]


@pytest.mark.parametrize(
    ("package", "path"), CASES, ids=[str(p.relative_to(APP_DIR)) for _, p in CASES]
)
def test_layer_does_not_import_forbidden_modules(package: str, path: Path) -> None:
    violations = {
        module
        for module in imported_modules(path)
        for forbidden in FORBIDDEN_IMPORTS[package]
        if module == forbidden or module.startswith(f"{forbidden}.")
    }

    assert not violations, f"{path.relative_to(APP_DIR)} imports {sorted(violations)}"


def test_routers_do_not_query_the_database_directly() -> None:
    for path in (APP_DIR / "api").rglob("*.py"):
        modules = imported_modules(path)
        assert "sqlalchemy" not in modules, path.name
        assert not any(m.startswith("app.repositories") for m in modules), path.name
        assert not any(m.startswith("app.models") for m in modules), path.name
