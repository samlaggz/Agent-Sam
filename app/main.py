from fastapi import Depends, FastAPI

from app.asyncio_compat import configure_windows_event_loop_policy
from app.api.routes.health import router as health_router
from app.config import Settings
from app.dependencies import settings_dependency
from db.session import AsyncSessionLocal
from gateways.service import AgentGatewayService
from gateways.webhook.router import build_webhook_router


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Sam API", version="0.1.0")
    settings = settings_dependency()

    @app.get("/", summary="Service metadata")
    async def root(settings: Settings = Depends(settings_dependency)) -> dict[str, str]:
        return {
            "name": settings.app_name,
            "environment": settings.environment,
            "message": "Agent Sam API skeleton",
        }

    app.include_router(health_router, prefix="/health", tags=["health"])
    if "webhook" in settings.enabled_gateways:
        app.include_router(
            build_webhook_router(settings, AgentGatewayService(AsyncSessionLocal)),
            prefix="/gateways",
            tags=["gateways"],
        )
    # TODO: Register versioned API routers as the platform surface expands.
    return app


app = create_app()


def run() -> None:
    import uvicorn

    from app.config import get_settings

    configure_windows_event_loop_policy()
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment == "development",
    )


if __name__ == "__main__":
    run()