# Model Routing

Model routing happens in two stages.

1. `RouterAgent` selects the specialist agent.
2. `BudgetService` approves, downgrades, or escalates the chosen model.

Execution goes through LiteLLM and can target OpenRouter with model strings like:

```text
openrouter/openai/gpt-4.1-mini
openrouter/anthropic/claude-3.7-sonnet
openrouter/google/gemini-2.0-flash-001
```

Useful commands:

```powershell
python -m scripts.models list
python -m scripts.models validate
python -m scripts.models validate --live
```