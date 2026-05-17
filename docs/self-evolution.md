# Self Evolution

Self-improvement is controlled and approval-gated.

Skill updates:

- agents can propose skill changes
- approval is required before activation
- approved skills publish through the existing skill system

Sub-agent proposals:

- agents can propose focused specialists with their own profile and prompt
- dangerous tools are blocked unless explicit admin approval is attached to the proposal metadata
- approved proposals can be written into `agents/configs/generated/` and `agents/prompts/generated/`

Learning events:

- completed specialist runs can emit summarized learning events into `learning_events`
- learned observations stay separate from active executable skills until they are explicitly promoted