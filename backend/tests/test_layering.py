"""Static enforcement for the versioned core/game dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path

PET_ROOT = Path(__file__).parents[1] / "src" / "pet"
CORE_ROOT = PET_ROOT / "core"
GAMES_ROOT = PET_ROOT / "games"
ALLOWED_CORE_MODULES = {"adapter_api", "config", "llm", "prompt"}


def _imports(path: Path) -> tuple[tuple[str, int], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[str, int]] = []
    relative = path.relative_to(PET_ROOT)
    package = ["pet", *relative.parts[:-1]]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module is not None:
                imports.append((node.module, node.lineno))
                continue
            ancestor = package[: len(package) - max(0, node.level - 1)]
            if node.module is not None:
                imports.append((".".join([*ancestor, node.module]), node.lineno))
            else:
                imports.extend(
                    (".".join([*ancestor, alias.name]), node.lineno)
                    for alias in node.names
                )
    return tuple(imports)


def _failure(path: Path, line: int, message: str) -> str:
    return f"{path.relative_to(PET_ROOT)}:{line}: {message}"


def test_core_never_imports_game_packages() -> None:
    failures = [
        _failure(path, line, f"core imports game module {module}")
        for path in CORE_ROOT.rglob("*.py")
        for module, line in _imports(path)
        if module == "pet.games" or module.startswith("pet.games.")
    ]
    assert not failures, "\n".join(failures)


def test_game_packages_do_not_cross_import_or_reach_eval_from_production() -> None:
    failures: list[str] = []
    for path in GAMES_ROOT.rglob("*.py"):
        relative = path.relative_to(GAMES_ROOT)
        if len(relative.parts) < 2:
            continue
        game_id = relative.parts[0]
        in_eval = len(relative.parts) >= 3 and relative.parts[1] == "eval"
        for module, line in _imports(path):
            prefix = "pet.games."
            if module.startswith(prefix):
                imported_parts = module.removeprefix(prefix).split(".")
                imported_game = imported_parts[0]
                if imported_game != game_id:
                    failures.append(
                        _failure(
                            path,
                            line,
                            f"game {game_id} imports other game {imported_game}",
                        )
                    )
                elif not in_eval and len(imported_parts) > 1 and imported_parts[1] == "eval":
                    failures.append(
                        _failure(path, line, "production game module imports its eval package")
                    )
            if module == "pet.core" or module.startswith("pet.core."):
                imported_core = module.split(".")[2] if module.count(".") >= 2 else ""
                allowed = set(ALLOWED_CORE_MODULES)
                if in_eval:
                    allowed.add("gate")
                if imported_core not in allowed:
                    failures.append(
                        _failure(
                            path,
                            line,
                            f"game module imports forbidden core module {module}",
                        )
                    )
    assert not failures, "\n".join(failures)


def test_pet_top_level_contains_only_package_and_composition_root() -> None:
    actual = {path.name for path in PET_ROOT.glob("*.py")}
    assert actual == {"__init__.py", "main.py"}
