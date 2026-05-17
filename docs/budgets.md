# Budgets

Budget controls live in `services/budget_service.py` and apply before a specialist run starts.

Controls:

- `MAX_COST_PER_TASK_USD`
- `DAILY_MODEL_BUDGET_USD`
- `ALLOW_MODEL_ESCALATION`

Behavior:

- low-confidence or failed attempts can escalate to a stronger model
- if the requested model exceeds the task or daily budget, the service tries a cheaper fallback
- if no affordable fallback exists, the decision is marked as requiring approval

Budget usage is stored in `model_budgets` and model-call cost data is stored in `model_calls`.