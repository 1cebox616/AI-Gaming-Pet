"""Static enforcement for the versioned core/game dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path

PET_ROOT = Path(__file__).parents[1] / "src" / "pet"
CORE_ROOT = PET_ROOT / "core"
GAMES_ROOT = PET_ROOT / "games"
BELIEF_ROOT = CORE_ROOT / "belief"
OCR_MODULES = tuple(CORE_ROOT.glob("ocr_*.py"))
LOCAL_SENSOR_PATHS = (CORE_ROOT / "capture.py", CORE_ROOT / "input_telemetry.py")
ALLOWED_CORE_MODULES = {"adapter_api", "config", "llm", "prompt"}
NETWORK_MODULES = {
    "aiohttp",
    "fastapi",
    "http",
    "httpx",
    "requests",
    "socket",
    "urllib",
    "urllib3",
    "websockets",
}


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


def _network_import_failures(paths: tuple[Path, ...], label: str) -> list[str]:
    return [
        _failure(path, line, f"{label} imports network module {module}")
        for path in paths
        for module, line in _imports(path)
        if module.split(".", maxsplit=1)[0] in NETWORK_MODULES
    ]


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
                if game_id == "generic":
                    allowed.update(
                        {"belief", "ocr_probe", "ocr_rapid", "ocr_selective"}
                    )
                if in_eval:
                    allowed.add("gate")
                if relative.as_posix() in {
                    "generic/eval/region_assets.py",
                    "generic/eval/observation_replay.py",
                    "generic/eval/ocr_ingame_probe.py",
                }:
                    allowed.add("capture")
                if relative.as_posix() == "generic/eval/observation_replay.py":
                    allowed.add("input_telemetry")
                if imported_core not in allowed:
                    failures.append(
                        _failure(
                            path,
                            line,
                            f"game module imports forbidden core module {module}",
                        )
                    )
    assert not failures, "\n".join(failures)


def test_belief_package_has_no_network_imports() -> None:
    failures = _network_import_failures(tuple(BELIEF_ROOT.rglob("*.py")), "belief")
    assert not failures, "\n".join(failures)


def test_ocr_modules_have_no_network_imports() -> None:
    failures = _network_import_failures(OCR_MODULES, "OCR module")
    assert not failures, "\n".join(failures)


def test_capture_and_input_telemetry_have_no_network_imports() -> None:
    failures = _network_import_failures(LOCAL_SENSOR_PATHS, "local sensor")
    assert not failures, "\n".join(failures)


def test_pet_top_level_contains_only_package_and_composition_root() -> None:
    actual = {path.name for path in PET_ROOT.glob("*.py")}
    assert actual == {"__init__.py", "main.py"}
