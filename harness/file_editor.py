from __future__ import annotations

import difflib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4

from harness.exceptions import FilePatchError, WorkspaceBoundaryError


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileEditor:
    def __init__(self, workspace_root: str | Path, patches_dir: str | Path | None = None) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._patches_dir = Path(patches_dir or self._workspace_root / "patches").resolve()
        self._patches_dir.mkdir(parents=True, exist_ok=True)

    def read_file(self, path: str) -> str:
        resolved = self._resolve(path)
        return resolved.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str, *, dry_run: bool = False) -> dict[str, str | bool | None]:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        old_content = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
        diff = self.generate_diff(old_content, content, path)
        patch_id = uuid4().hex
        backup_path = self.create_backup(path) if resolved.exists() and not dry_run else None
        if not dry_run:
            resolved.write_text(content, encoding="utf-8")
            self._store_patch_record(patch_id, path, diff, backup_path)
        return {"path": str(resolved), "patch_id": patch_id, "diff": diff, "changed": old_content != content}

    def apply_patch(self, diff: str, *, dry_run: bool = False) -> dict[str, str | bool | None]:
        target_path = self._extract_target_path(diff)
        resolved = self._resolve(target_path)
        old_content = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
        new_content = self._apply_unified_diff(old_content, diff)
        patch_id = uuid4().hex
        backup_path = self.create_backup(target_path) if resolved.exists() and not dry_run else None
        if not dry_run:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(new_content, encoding="utf-8")
            self._store_patch_record(patch_id, target_path, diff, backup_path)
        return {"path": str(resolved), "patch_id": patch_id, "diff": diff, "changed": old_content != new_content}

    def create_backup(self, path: str) -> str | None:
        resolved = self._resolve(path)
        if not resolved.exists():
            return None
        backup_dir = self._patches_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{uuid4().hex}_{resolved.name}.bak"
        shutil.copy2(resolved, backup_path)
        return str(backup_path)

    def rollback_patch(self, patch_id: str) -> str:
        metadata_path = self._patches_dir / f"{patch_id}.json"
        if not metadata_path.exists():
            raise FileNotFoundError(f"Patch record does not exist: {patch_id}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target_path = self._resolve(str(metadata["path"]))
        backup_path = metadata.get("backup_path")
        if not backup_path:
            if target_path.exists():
                target_path.unlink()
            return str(target_path)
        shutil.copy2(backup_path, target_path)
        return str(target_path)

    def generate_diff(self, old: str, new: str, path: str) -> str:
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=path,
                tofile=path,
                lineterm="",
            )
        )
        return "\n".join(diff_lines) + ("\n" if diff_lines else "")

    def _resolve(self, path: str) -> Path:
        raw_path = Path(path)
        resolved = raw_path.resolve() if raw_path.is_absolute() else (self._workspace_root / raw_path).resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise WorkspaceBoundaryError(f"Path is outside the workspace root: {resolved}") from exc
        return resolved

    def _store_patch_record(self, patch_id: str, path: str, diff: str, backup_path: str | None) -> None:
        diff_path = self._patches_dir / f"{patch_id}.diff"
        metadata_path = self._patches_dir / f"{patch_id}.json"
        diff_path.write_text(diff, encoding="utf-8")
        metadata_path.write_text(
            json.dumps({"path": path, "backup_path": backup_path, "diff_path": str(diff_path)}, indent=2),
            encoding="utf-8",
        )

    def _extract_target_path(self, diff: str) -> str:
        for line in diff.splitlines():
            if line.startswith("+++"):
                return line[4:].strip().replace("b/", "", 1)
        raise FilePatchError("Unified diff does not contain a target path.")

    def _apply_unified_diff(self, original_text: str, diff: str) -> str:
        source_lines = original_text.splitlines()
        diff_lines = diff.splitlines()
        result: list[str] = []
        source_index = 0
        index = 0

        while index < len(diff_lines) and not diff_lines[index].startswith("@@"):
            index += 1

        while index < len(diff_lines):
            header = diff_lines[index]
            match = HUNK_RE.match(header)
            if match is None:
                raise FilePatchError(f"Invalid unified diff hunk header: {header}")
            start_old = int(match.group(1)) - 1
            while source_index < start_old:
                result.append(source_lines[source_index])
                source_index += 1
            index += 1
            while index < len(diff_lines) and not diff_lines[index].startswith("@@"):
                line = diff_lines[index]
                if not line:
                    prefix = " "
                    value = ""
                else:
                    prefix = line[0]
                    value = line[1:]
                if prefix == " ":
                    if source_index >= len(source_lines):
                        raise FilePatchError("Unified diff context exceeds source length.")
                    result.append(source_lines[source_index])
                    source_index += 1
                elif prefix == "-":
                    source_index += 1
                elif prefix == "+":
                    result.append(value)
                elif prefix == "\\":
                    pass
                else:
                    raise FilePatchError(f"Unsupported diff line: {line}")
                index += 1

        result.extend(source_lines[source_index:])
        trailing_newline = original_text.endswith("\n") or "+" in diff
        content = "\n".join(result)
        if trailing_newline and content and not content.endswith("\n"):
            content += "\n"
        return content