from __future__ import annotations

from pathlib import Path

import pytest

from harness.file_editor import FileEditor


def test_file_editor_applies_patch_and_rolls_back(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    editor = FileEditor(workspace_root)
    file_path = workspace_root / "sample.txt"
    file_path.write_text("hello\nworld\n", encoding="utf-8")

    diff = editor.generate_diff("hello\nworld\n", "hello\nagent\n", "sample.txt")
    result = editor.apply_patch(diff)

    assert file_path.read_text(encoding="utf-8") == "hello\nagent\n"
    editor.rollback_patch(str(result["patch_id"]))
    assert file_path.read_text(encoding="utf-8") == "hello\nworld\n"


def test_file_editor_write_creates_patch_record(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    editor = FileEditor(workspace_root)

    result = editor.write_file("new.txt", "content\n")

    assert Path(str(result["path"])).read_text(encoding="utf-8") == "content\n"
    assert (workspace_root / "patches" / f"{result['patch_id']}.diff").exists()