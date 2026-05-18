from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from app.config import Settings
from harness.exceptions import SourceCacheError


@dataclass(frozen=True)
class SourceCacheMatch:
    path: str
    line: int
    text: str


class SourceCacheService:
    def __init__(self, settings: Settings, *, cache_dir: str | Path | None = None) -> None:
        self._settings = settings
        self._cache_dir = Path(cache_dir or settings.source_cache_dir).resolve()

    @property
    def cache_dir(self) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def build_fetch_command(self, package_spec: str) -> list[str]:
        return [self._settings.opensrc_command, "fetch", package_spec]

    def build_path_command(self, package_spec: str) -> list[str]:
        return [self._settings.opensrc_command, "path", package_spec]

    def build_search_command(self, cache_path: str | Path, query: str) -> list[str]:
        return [self._settings.ripgrep_command, "-n", "--no-heading", query, str(cache_path)]

    def parse_search_output(self, output: str) -> list[SourceCacheMatch]:
        matches: list[SourceCacheMatch] = []
        for line in output.splitlines():
            parts = line.split(":", maxsplit=2)
            if len(parts) != 3:
                continue
            path, line_number, text = parts
            if not line_number.isdigit():
                continue
            matches.append(SourceCacheMatch(path=path, line=int(line_number), text=text))
        return matches

    def check_doctor(self) -> list[str]:
        failures: list[str] = []
        if shutil.which(self._settings.opensrc_command) is None:
            failures.append("opensrc command is unavailable")
        if shutil.which(self._settings.ripgrep_command) is None:
            failures.append("ripgrep command is unavailable")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.cache_dir.exists() or not os_access_writable(self.cache_dir):
            failures.append("source cache directory is not writable")
        return failures

    def path(self, package_spec: str) -> Path:
        result = subprocess.run(
            self.build_path_command(package_spec),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SourceCacheError(result.stderr.strip() or result.stdout.strip() or "opensrc path failed")
        return Path(result.stdout.strip())

    def fetch(self, package_spec: str) -> Path:
        result = subprocess.run(
            self.build_fetch_command(package_spec),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SourceCacheError(result.stderr.strip() or result.stdout.strip() or "opensrc fetch failed")
        return self.path(package_spec)

    def search(self, package_spec: str, query: str) -> list[SourceCacheMatch]:
        cache_path = self.path(package_spec)
        result = subprocess.run(
            self.build_search_command(cache_path, query),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise SourceCacheError(result.stderr.strip() or result.stdout.strip() or "source cache search failed")
        return self.parse_search_output(result.stdout)

    def read(self, path: str | Path) -> str:
        resolved = Path(path).resolve()
        return resolved.read_text(encoding="utf-8")

    def summarize_usage_examples(self, package_spec: str, query: str) -> str:
        matches = self.search(package_spec, query)
        if not matches:
            return f"No matches found for {query!r} in {package_spec}."
        preview = matches[:5]
        return "\n".join(f"{match.path}:{match.line}: {match.text}" for match in preview)


def os_access_writable(path: Path) -> bool:
    try:
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False