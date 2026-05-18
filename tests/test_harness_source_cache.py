from __future__ import annotations

from app.config import Settings
from harness.source_cache import SourceCacheService


def test_source_cache_builds_commands_and_parses_search_output(tmp_path) -> None:
    settings = Settings(source_cache_dir=str(tmp_path / "cache"), opensrc_command="opensrc", ripgrep_command="rg")
    service = SourceCacheService(settings)

    assert service.build_fetch_command("pypi:requests") == ["opensrc", "fetch", "pypi:requests"]
    assert service.build_path_command("zod") == ["opensrc", "path", "zod"]
    assert service.build_search_command(tmp_path / "cache" / "requests", "Session") == [
        "rg",
        "-n",
        "--no-heading",
        "Session",
        str(tmp_path / "cache" / "requests"),
    ]

    matches = service.parse_search_output("pkg/file.py:12:class Session:\npkg/other.py:4:Session()")
    assert matches[0].path == "pkg/file.py"
    assert matches[0].line == 12
    assert matches[1].text == "Session()"