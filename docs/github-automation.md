# GitHub Automation

The harness GitHub layer supports branch creation, commits, pushes, and pull request operations without bypassing approval policy.

## Settings

```env
GITHUB_TOKEN=
GITHUB_REPOSITORY=
GITHUB_DEFAULT_BRANCH=main
GITHUB_BOT_BRANCH_PREFIX=agent-sam/
GITHUB_AUTO_CREATE_PR=false
```

## Guardrails

- protected branch operations are blocked
- force push is not implemented
- automatic PR creation still requires `GITHUB_TOKEN`
- tokens are redacted from logs and responses

Use `/pr <task_id>` to queue a PR-preparation follow-up task.