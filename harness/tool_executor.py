from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.memory_service import MemorySearchRequest, search_memories
from db.models import Approval, Task
from db.repositories import log_tool_call
from harness.actions import ApprovalAction, BrowserAction, FilePatchAction, FileReadAction, FileWriteAction, FinishAction, GitAction, GitHubAction, InstallLibraryAction, InstallRepoAction, SearchAction, ShellAction, SourceCacheAction
from harness.events import ApprovalRequestedEvent, ApprovalResolvedEvent, BrowserActionEvent, ErrorEvent, EventStore, FileEditEvent, ShellCommandEvent, ToolCallEvent, ToolResultEvent
from harness.github import GitHubService
from harness.observations import ApprovalObservation, BrowserObservation, ErrorObservation, FileObservation, GitHubObservation, GitObservation, Observation, ShellObservation, SourceCacheObservation
from harness.policies import HarnessPolicies
from harness.runtime import LocalRuntime
from harness.source_cache import SourceCacheService
from harness.tool_registry import ToolRegistry, ToolSpec
from harness.workspace import WorkspacePaths
from tools.browser_tool import BrowserTool


@dataclass(slots=True)
class ToolExecutionContext:
    task_id: UUID
    agent_run_id: UUID | None
    agent_slug: str
    workspace: WorkspacePaths
    runtime: LocalRuntime


class ToolExecutor:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        event_store: EventStore,
        *,
        registry: ToolRegistry | None = None,
        policies: HarnessPolicies | None = None,
        browser_tool: BrowserTool | None = None,
        source_cache: SourceCacheService | None = None,
        github: GitHubService | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._event_store = event_store
        self._registry = registry or ToolRegistry()
        self._policies = policies or HarnessPolicies(settings)
        self._browser_tool = browser_tool or BrowserTool(session_factory)
        self._source_cache = source_cache or SourceCacheService(settings)
        self._github = github or GitHubService(settings)
        self._register_builtin_tools()

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def execute_action(
        self,
        action,
        *,
        task_id: UUID,
        agent_run_id: UUID | None,
        agent_slug: str,
        workspace: WorkspacePaths,
    ) -> Observation:
        if isinstance(action, FinishAction):
            return Observation(summary=action.summary)
        runtime = LocalRuntime(self._settings, workspace, task_id=task_id, session_factory=self._session_factory)
        context = ToolExecutionContext(
            task_id=task_id,
            agent_run_id=agent_run_id,
            agent_slug=agent_slug,
            workspace=workspace,
            runtime=runtime,
        )
        decision = self._policies.evaluate(action, workspace_root=workspace.root)
        tool_name = self._tool_name_for_action(action)
        await self._event_store.append(
            ToolCallEvent(
                task_id=task_id,
                run_id=agent_run_id,
                agent_slug=agent_slug,
                content=f"{tool_name}: {action.action_type}",
                metadata={"action": json.loads(json.dumps(action.model_dump(mode="json"), default=str))},
            )
        )
        if not decision.allowed:
            error = ErrorObservation(error_type="policy_violation", error_message=decision.reason, summary=decision.reason)
            await self._event_store.append(
                ErrorEvent(task_id=task_id, run_id=agent_run_id, agent_slug=agent_slug, content=decision.reason)
            )
            return error
        if decision.requires_approval:
            approval_id = await self._create_approval(task_id=task_id, tool_name=tool_name, reason=decision.reason, payload=action.model_dump(mode="json"))
            await self._event_store.append(
                ApprovalRequestedEvent(
                    task_id=task_id,
                    run_id=agent_run_id,
                    agent_slug=agent_slug,
                    content=decision.reason,
                    metadata={"approval_id": str(approval_id), "tool_name": tool_name},
                )
            )
            return ApprovalObservation(summary=decision.reason, approval_id=approval_id, status="pending")

        tool = self._registry.get_tool(tool_name)
        validated_action = self._registry.validate_tool_input(tool_name, action.model_dump(mode="json"))
        try:
            observation = await tool.handler(validated_action, context)
        except Exception as exc:
            await self._event_store.append(
                ErrorEvent(
                    task_id=task_id,
                    run_id=agent_run_id,
                    agent_slug=agent_slug,
                    content=str(exc),
                    metadata={"tool_name": tool_name},
                )
            )
            return ErrorObservation(error_type=type(exc).__name__, error_message=str(exc), summary=str(exc))

        await self._record_tool_call(tool_name=tool_name, task_id=task_id, observation=observation, action_payload=action.model_dump(mode="json"))
        await self._event_store.append(
            ToolResultEvent(
                task_id=task_id,
                run_id=agent_run_id,
                agent_slug=agent_slug,
                content=observation.summary,
                metadata={"tool_name": tool_name, "observation": observation.model_dump(mode="json")},
            )
        )
        return observation

    async def _exec_shell(self, action: ShellAction, context: ToolExecutionContext) -> ShellObservation:
        result = await context.runtime.run_shell(action.command, cwd=action.cwd, timeout=action.timeout_seconds)
        await self._event_store.append(
            ShellCommandEvent(
                task_id=context.task_id,
                run_id=context.agent_run_id,
                agent_slug=context.agent_slug,
                content=action.command,
                metadata={"cwd": result.cwd, "status": result.status},
            )
        )
        return ShellObservation(
            summary=result.stdout.strip() or result.stderr.strip() or result.status,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            status=result.status,
            tool_call_id=result.tool_call_id,
            approval_id=result.approval_id,
            success=result.status == "completed" and (result.exit_code in (0, None)),
        )

    async def _exec_browser(self, action: BrowserAction, context: ToolExecutionContext) -> BrowserObservation:
        task_key = str(context.task_id)
        if action.operation == "open":
            result = await self._browser_tool.navigate(task_id=task_key, url=str(action.url))
        elif action.operation in {"snapshot", "extract_text"}:
            result = await self._browser_tool.snapshot(task_id=task_key, full=action.operation == "extract_text")
        elif action.operation == "click":
            result = await self._browser_tool.click(task_id=task_key, ref=str(action.ref))
        elif action.operation == "type":
            result = await self._browser_tool.type_text(task_id=task_key, ref=str(action.ref), text=str(action.text or ""))
        elif action.operation == "press":
            result = await self._browser_tool.press_key(task_id=task_key, key=str(action.key or "Enter"))
        elif action.operation == "scroll":
            result = await self._browser_tool.scroll(task_id=task_key, direction=str(action.direction or "down"))
        elif action.operation == "close":
            await self._browser_tool.close_session(task_key)
            result = type("Closed", (), {"success": True, "data": {"url": None, "title": None}, "error": None})()
        else:
            raise ValueError(f"Unsupported browser operation: {action.operation}")
        snapshot_text = str(result.data.get("snapshot", "") or result.data.get("text", ""))
        captcha_detected = "captcha" in snapshot_text.lower() or "i'm not a robot" in snapshot_text.lower()
        await self._event_store.append(
            BrowserActionEvent(
                task_id=context.task_id,
                run_id=context.agent_run_id,
                agent_slug=context.agent_slug,
                content=action.operation,
                metadata={"url": result.data.get("url"), "captcha_detected": captcha_detected},
            )
        )
        if captcha_detected:
            return BrowserObservation(
                success=False,
                summary="CAPTCHA detected; human handoff required.",
                url=result.data.get("url"),
                title=result.data.get("title"),
                text=snapshot_text,
                captcha_detected=True,
            )
        return BrowserObservation(
            success=bool(result.success),
            summary=str(result.data.get("title") or result.data.get("url") or action.operation),
            url=result.data.get("url"),
            title=result.data.get("title"),
            text=snapshot_text,
            captcha_detected=False,
        )

    async def _exec_file_read(self, action: FileReadAction, context: ToolExecutionContext) -> FileObservation:
        content = context.runtime.read_file(action.path)
        return FileObservation(summary=f"Read {action.path}", path=action.path, content=content, changed=False)

    async def _exec_file_write(self, action: FileWriteAction, context: ToolExecutionContext) -> FileObservation:
        result = context.runtime.write_file(action.path, action.content)
        await self._event_store.append(
            FileEditEvent(
                task_id=context.task_id,
                run_id=context.agent_run_id,
                agent_slug=context.agent_slug,
                content=action.path,
                metadata={"patch_id": result.get("patch_id")},
            )
        )
        return FileObservation(
            summary=f"Wrote {action.path}",
            path=action.path,
            patch_id=str(result.get("patch_id")),
            changed=bool(result.get("changed")),
        )

    async def _exec_file_patch(self, action: FilePatchAction, context: ToolExecutionContext) -> FileObservation:
        result = context.runtime.apply_patch(action.diff)
        await self._event_store.append(
            FileEditEvent(
                task_id=context.task_id,
                run_id=context.agent_run_id,
                agent_slug=context.agent_slug,
                content=str(result.get("path")),
                metadata={"patch_id": result.get("patch_id")},
            )
        )
        return FileObservation(
            summary=f"Patched {result.get('path')}",
            path=str(result.get("path")),
            patch_id=str(result.get("patch_id")),
            changed=bool(result.get("changed")),
        )

    async def _exec_search(self, action: SearchAction, context: ToolExecutionContext) -> FileObservation:
        base = context.workspace.repo if action.path is None else (context.workspace.root / action.path)
        matches: list[str] = []
        for candidate in sorted(base.rglob("*")):
            if not candidate.is_file():
                continue
            try:
                for index, line in enumerate(candidate.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                    if action.query.lower() in line.lower():
                        matches.append(f"{candidate.relative_to(context.workspace.root)}:{index}:{line.strip()}")
                        if len(matches) >= action.max_results:
                            break
                if len(matches) >= action.max_results:
                    break
            except OSError:
                continue
        return FileObservation(summary=f"Found {len(matches)} matches for {action.query}", content="\n".join(matches), changed=False)

    async def _exec_install_library(self, action: InstallLibraryAction, context: ToolExecutionContext) -> ShellObservation:
        result = await context.runtime.install_library(
            action.package_name,
            ecosystem=action.ecosystem,
            version=action.version,
            scope=action.scope,
        )
        return ShellObservation(
            summary=result.stdout.strip() or result.stderr.strip() or result.status,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            status=result.status,
            success=result.status == "completed" and (result.exit_code in (0, None)),
        )

    async def _exec_install_repo(self, action: InstallRepoAction, context: ToolExecutionContext) -> ShellObservation:
        result = await context.runtime.clone_repo(action.repo_url, destination=action.destination)
        return ShellObservation(
            summary=result.stdout.strip() or result.stderr.strip() or result.status,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            status=result.status,
            success=result.status == "completed" and (result.exit_code in (0, None)),
        )

    async def _exec_git(self, action: GitAction, context: ToolExecutionContext) -> GitObservation:
        if action.operation == "status":
            output = self._github.git_status(Path(context.workspace.repo if action.cwd is None else context.workspace.root / action.cwd))
            return GitObservation(summary="git status", output=output)
        if action.operation == "branch":
            branch_name = action.args[0] if action.args else f"{self._settings.github_bot_branch_prefix}{context.task_id}"
            output = self._github.create_branch(context.workspace.repo, branch_name)
            return GitObservation(summary=f"Created branch {branch_name}", branch=branch_name, output=output)
        if action.operation == "commit":
            output = self._github.commit_changes(context.workspace.repo, action.message or "Agent_Sam update")
            return GitObservation(summary="Committed changes", output=output)
        if action.operation == "push":
            branch_name = action.args[0] if action.args else ""
            output = self._github.push_branch(context.workspace.repo, branch_name)
            return GitObservation(summary=f"Pushed {branch_name}", branch=branch_name, output=output)
        shell = await context.runtime.run_shell(f"git {action.operation} {' '.join(action.args)}", cwd=action.cwd)
        return GitObservation(summary=shell.stdout.strip() or shell.stderr.strip(), output=shell.stdout or shell.stderr)

    async def _exec_github(self, action: GitHubAction, context: ToolExecutionContext) -> GitHubObservation:
        if action.operation == "open_pr":
            payload = self._github.open_pull_request(**action.payload)
        elif action.operation == "comment_pr":
            payload = self._github.comment_on_pr(**action.payload)
        elif action.operation == "read_issue":
            payload = self._github.read_issue(**action.payload)
        else:
            raise ValueError(f"Unsupported GitHub action: {action.operation}")
        return GitHubObservation(
            summary=str(payload.get("html_url") or payload.get("title") or action.operation),
            url=payload.get("html_url"),
            number=payload.get("number"),
            state=payload.get("state"),
            payload=payload,
        )

    async def _exec_source_cache(self, action: SourceCacheAction, context: ToolExecutionContext) -> SourceCacheObservation:
        if action.operation == "path":
            cache_path = self._source_cache.path(action.package_spec)
            return SourceCacheObservation(summary=str(cache_path), cache_path=str(cache_path))
        if action.operation == "fetch":
            cache_path = self._source_cache.fetch(action.package_spec)
            return SourceCacheObservation(summary=f"Fetched {action.package_spec}", cache_path=str(cache_path))
        if action.operation == "search":
            matches = self._source_cache.search(action.package_spec, str(action.query or ""))
            return SourceCacheObservation(
                summary=f"Found {len(matches)} cached source matches",
                cache_path=str(self._source_cache.path(action.package_spec)),
                matches=[match.__dict__ for match in matches],
            )
        if action.operation == "read":
            content = self._source_cache.read(str(action.path or ""))
            return SourceCacheObservation(summary=f"Read {action.path}", matches=[{"path": str(action.path), "line": 1, "text": content[:400]}])
        summary = self._source_cache.summarize_usage_examples(action.package_spec, str(action.query or ""))
        return SourceCacheObservation(summary=summary)

    async def _exec_approval(self, action: ApprovalAction, context: ToolExecutionContext) -> ApprovalObservation:
        async with self._session_factory() as session:
            approval = await session.get(Approval, action.approval_id)
            if approval is None:
                raise ValueError(f"Approval {action.approval_id} does not exist.")
            approval.status = "approved" if action.decision == "approve" else "rejected"
            if approval.tool_call is not None and action.decision == "approve":
                approval.tool_call.approved_by_user = True
            await session.commit()
        await self._event_store.append(
            ApprovalResolvedEvent(
                task_id=context.task_id,
                run_id=context.agent_run_id,
                agent_slug=context.agent_slug,
                content=action.decision,
                metadata={"approval_id": str(action.approval_id)},
            )
        )
        return ApprovalObservation(summary=action.decision, approval_id=action.approval_id, status="approved" if action.decision == "approve" else "rejected")

    async def _exec_memory_search(self, query: str, task_id: UUID) -> FileObservation:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            results = await search_memories(
                session,
                MemorySearchRequest(workspace_id=task.workspace_id if task else None, text_query=query, limit=5),
            )
        content = "\n".join(memory.content for memory in results)
        return FileObservation(summary=f"Found {len(results)} memories", content=content)

    async def _record_tool_call(self, *, tool_name: str, task_id: UUID, observation: Observation, action_payload: dict[str, Any]) -> None:
        if tool_name == "safe_shell" and isinstance(observation, ShellObservation) and observation.tool_call_id is not None:
            return
        async with self._session_factory() as session:
            await log_tool_call(
                session,
                tool_name=tool_name,
                input_payload=action_payload,
                output_text=observation.summary,
                stderr_text=getattr(observation, "stderr", None),
                status="completed" if observation.success else "failed",
                risk_level="safe",
                task_id=task_id,
            )

    async def _create_approval(self, *, task_id: UUID, tool_name: str, reason: str, payload: dict[str, Any]) -> UUID:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")
            approval = Approval(
                workspace_id=task.workspace_id,
                task_id=task.id,
                requested_by_user_id=task.created_by_user_id,
                reason=reason,
                metadata_json={"tool_name": tool_name, "payload": payload},
            )
            session.add(approval)
            await session.commit()
            await session.refresh(approval)
            return approval.id

    def _register_builtin_tools(self) -> None:
        tools = (
            ToolSpec("safe_shell", "Safe shell execution", ShellAction, ShellObservation, self._exec_shell, workspace_required=True),
            ToolSpec("browser_tool", "Browser automation", BrowserAction, BrowserObservation, self._exec_browser, workspace_required=True, network_required=True),
            ToolSpec("file_read", "Read a file", FileReadAction, FileObservation, self._exec_file_read, workspace_required=True),
            ToolSpec("file_write", "Write a file", FileWriteAction, FileObservation, self._exec_file_write, workspace_required=True),
            ToolSpec("file_patch", "Apply a file patch", FilePatchAction, FileObservation, self._exec_file_patch, workspace_required=True),
            ToolSpec("search", "Search workspace text", SearchAction, FileObservation, self._exec_search, workspace_required=True),
            ToolSpec("install_library", "Install a project library", InstallLibraryAction, ShellObservation, self._exec_install_library, workspace_required=True, network_required=True),
            ToolSpec("install_repo", "Clone a repository", InstallRepoAction, ShellObservation, self._exec_install_repo, workspace_required=True, network_required=True),
            ToolSpec("git", "Git operations", GitAction, GitObservation, self._exec_git, workspace_required=True),
            ToolSpec("github", "GitHub operations", GitHubAction, GitHubObservation, self._exec_github, workspace_required=True, network_required=True),
            ToolSpec("source_cache", "Offline source cache", SourceCacheAction, SourceCacheObservation, self._exec_source_cache, workspace_required=True, network_required=True),
            ToolSpec("approval", "Resolve approval state", ApprovalAction, ApprovalObservation, self._exec_approval, workspace_required=False),
        )
        for tool in tools:
            self._registry.register_tool(tool)

    def _tool_name_for_action(self, action) -> str:
        return {
            "shell": "safe_shell",
            "browser": "browser_tool",
            "file_read": "file_read",
            "file_write": "file_write",
            "file_patch": "file_patch",
            "search": "search",
            "install_library": "install_library",
            "install_repo": "install_repo",
            "git": "git",
            "github": "github",
            "source_cache": "source_cache",
            "approval": "approval",
        }.get(action.action_type, action.action_type)