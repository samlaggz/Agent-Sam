# OpenRouter

Agent_Sam uses LiteLLM as the execution layer and can route specialist prompts through OpenRouter.

Required env values for OpenRouter-backed models:

- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL`

Recommended defaults:

```text
LITELLM_MODEL=openrouter/openai/gpt-4.1-mini
DEFAULT_MODEL=openrouter/openai/gpt-4.1-mini
```

The doctor validates OpenRouter model syntax and checks that credentials are present when any configured model uses the `openrouter/` prefix.