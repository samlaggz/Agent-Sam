from __future__ import annotations

import subprocess
import sys


def test_source_cache_help_displays_usage() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.source_cache", "--help"],
        cwd="F:\\personal\\Agent_Sam",
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "Manage the Agent_Sam offline source cache." in completed.stdout
    assert "{fetch,path,search}" in completed.stdout