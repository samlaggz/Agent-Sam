from fastapi import Depends, FastAPI

from app.api.routes.health import router as health_router
from app.config import Settings
from app.dependencies import settings_dependency


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Sam API", version="0.1.0")

    @app.get("/", summary="Service metadata")
    async def root(settings: Settings = Depends(settings_dependency)) -> dict[str, str]:
        return {
            "name": settings.app_name,
            "environment": settings.environment,
            "message": "Agent Sam API skeleton",
        }

    app.include_router(health_router, prefix="/health", tags=["health"])
    # TODO: Register versioned API routers as the platform surface expands.
    return app


app = create_app()


def run() -> None:
    import uvicorn

    from app.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment == "development",
    )