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

    # The hermes client should include web tools in its tool definitions
    tool_defs = model.hermes_client.get_tool_definitions()
    tool_names = [t["function"]["name"] for t in tool_defs]

    assert "web_search" in tool_names
    assert "web_open" in tool_names