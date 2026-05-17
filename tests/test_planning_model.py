from uuid import uuid4

from agent.model_client import LiteLLMPlanningModel
from agents.registry import get_agent_profile
from app.config import Settings


def test_planning_model_includes_web_tools_for_research_agent(session_factory) -> None:
    model = LiteLLMPlanningModel(
        Settings(_env_file=None, enable_web_research=True, openrouter_api_key="test-key"),
        session_factory,
        profile=get_agent_profile("research_agent"),
    )

    messages = model._build_messages(
        task={"id": str(uuid4()), "title": "Find latest docs", "description": "Need online research"},
        memory_context=[],
        skill_context=[],
    )

    assert "web_search" in messages[0]["content"]
    assert "web_open" in messages[0]["content"]