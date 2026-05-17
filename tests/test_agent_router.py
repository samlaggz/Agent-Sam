from agents.router import RouteRequest, RouterAgent


def test_router_selects_coding_agent_for_code_task() -> None:
    router = RouterAgent()

    decision = router.route(
        RouteRequest(
            title="Debug failing refactor in worker runtime",
            description="Need code fix and minimal implementation change",
        )
    )

    assert decision.agent_slug == "coding_agent"
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.model


def test_router_selects_server_ops_agent_for_nginx_task() -> None:
    router = RouterAgent()

    decision = router.route(
        RouteRequest(
            title="Fix nginx and systemd on Linux server",
            description="Production deployment and service status issue",
            safety_critical=True,
        )
    )

    assert decision.agent_slug == "server_ops_agent"
    assert decision.requires_human_approval is True


def test_router_selects_research_agent_for_latest_docs_task() -> None:
    router = RouterAgent()

    decision = router.route(
        RouteRequest(
            title="Find latest LiteLLM OpenRouter docs",
            description="Need documentation lookup and comparison",
        )
    )

    assert decision.agent_slug == "research_agent"
    assert decision.estimated_cost_level in {"low", "medium", "high"}


def test_router_selects_research_agent_for_internet_lookup_task() -> None:
    router = RouterAgent()

    decision = router.route(
        RouteRequest(
            title="Check the internet for the latest agent framework updates",
            description="Browse online sources and summarize what changed",
        )
    )

    assert decision.agent_slug == "research_agent"


def test_router_selects_server_ops_agent_for_find_on_server_task() -> None:
    router = RouterAgent()

    decision = router.route(
        RouteRequest(
            title="Find shivadrive in server",
            description="Search the server filesystem and running processes for shivadrive",
        )
    )

    assert decision.agent_slug == "server_ops_agent"


def test_router_selects_server_ops_agent_for_folder_lookup_task() -> None:
    router = RouterAgent()

    decision = router.route(
        RouteRequest(
            title="Can you find shivadrive folder?",
            description="Search server folders and site paths for shivadrive",
        )
    )

    assert decision.agent_slug == "server_ops_agent"