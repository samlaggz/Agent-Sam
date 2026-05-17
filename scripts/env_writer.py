from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


ENV_ASSIGNMENT_RE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$")
SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@,+\-]+$")


@dataclass(frozen=True)
class EnvWriteReport:
    path: Path
    updated_keys: tuple[str, ...]
    preserved_keys: tuple[str, ...]
    removed_duplicate_keys: tuple[str, ...]


def ensure_env_file(env_path: Path, *, example_path: Path | None = None) -> tuple[Path, bool]:
    if env_path.exists():
        return env_path, False

    source_path = _resolve_source_path(env_path, example_path)
    if source_path is not None and source_path.exists():
        env_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        env_path.write_text("", encoding="utf-8")
    return env_path, True


def load_env_values(env_path: Path, *, example_path: Path | None = None) -> dict[str, str]:
    return _parse_env_values(_load_env_lines(env_path, example_path=example_path))


def find_duplicate_keys(env_path: Path) -> tuple[str, ...]:
    if not env_path.exists():
        return ()

    duplicates: list[str] = []
    seen: set[str] = set()
    duplicate_seen: set[str] = set()
    for line in env_path.read_text(encoding="utf-8").splitlines(keepends=True):
        match = ENV_ASSIGNMENT_RE.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key in seen and key not in duplicate_seen:
            duplicates.append(key)
            duplicate_seen.add(key)
        seen.add(key)
    return tuple(duplicates)


def update_env_file(
    env_path: Path,
    updates: dict[str, str],
    *,
    example_path: Path | None = None,
    preserve_existing_values: bool = False,
) -> EnvWriteReport:
    lines = _load_env_lines(env_path, example_path=example_path)
    new_lines = list(lines)
    pending_updates = dict(updates)
    updated_keys: list[str] = []
    preserved_keys: list[str] = []
    removed_duplicate_keys: list[str] = []
    seen_keys: set[str] = set()
    duplicate_seen: set[str] = set()

    for index, line in enumerate(new_lines):
        match = ENV_ASSIGNMENT_RE.match(line)
        if match is None:
            continue

        key = match.group("key")
        if key in seen_keys:
            new_lines[index] = ""
            if key not in duplicate_seen:
                removed_duplicate_keys.append(key)
                duplicate_seen.add(key)
            continue

        seen_keys.add(key)
        if key not in pending_updates:
            continue

        current_value = _decode_env_value(line.split("=", maxsplit=1)[1].strip())
        if preserve_existing_values and current_value:
            preserved_keys.append(key)
            pending_updates.pop(key)
            continue

        line_ending = "\n" if line.endswith("\n") else ""
        prefix = match.group("prefix") or ""
        new_lines[index] = f"{prefix}{key}={format_env_value(pending_updates.pop(key))}{line_ending}"
        updated_keys.append(key)

    new_lines = [line for line in new_lines if line != ""]
    if pending_updates and new_lines and new_lines[-1] and not new_lines[-1].endswith("\n"):
        new_lines[-1] = new_lines[-1] + "\n"

    for key, value in pending_updates.items():
        new_lines.append(f"{key}={format_env_value(value)}\n")
        updated_keys.append(key)

    env_path.write_text("".join(new_lines), encoding="utf-8")
    return EnvWriteReport(
        path=env_path,
        updated_keys=tuple(updated_keys),
        preserved_keys=tuple(preserved_keys),
        removed_duplicate_keys=tuple(removed_duplicate_keys),
    )


def format_env_value(value: str) -> str:
    if value == "":
        return ""
    if SAFE_ENV_VALUE_RE.fullmatch(value):
        return value
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped_value}"'


def _resolve_source_path(env_path: Path, example_path: Path | None) -> Path | None:
    if example_path is not None:
        return example_path
    return env_path.parent / ".env.example"


def _load_env_lines(env_path: Path, *, example_path: Path | None = None) -> list[str]:
    if env_path.exists():
        return env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    source_path = _resolve_source_path(env_path, example_path)
    if source_path is not None and source_path.exists():
        return source_path.read_text(encoding="utf-8").splitlines(keepends=True)

    return []


def _parse_env_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        match = ENV_ASSIGNMENT_RE.match(line)
        if match is None:
            continue
        key = match.group("key")
        raw_value = line.split("=", maxsplit=1)[1].strip()
        values[key] = _decode_env_value(raw_value)
    return values


def _decode_env_value(raw_value: str) -> str:
    stripped = raw_value.strip()
    if not stripped:
        return ""
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            return stripped[1:-1]
        if isinstance(decoded, str):
            return decoded
    return stripped