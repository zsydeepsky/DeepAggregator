from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles


class NoCacheStaticFiles(StaticFiles):
    """静态文件协商缓存：浏览器每次加载都校验 etag，杜绝旧脚本/样式残留"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp



from app.config import settings
from app.core.api import CoreAPI
from app.core.appsettings import AppSettings
from app.core.services.archive_queue import ArchiveQueue
from app.core.services.embeddings import build_embedder
from app.core.services.jobs import JobManager
from app.core.workspaces import WorkspaceManager
from app.web.routes import router
from app.web.chats import router as chats_router
from app.web.ai_media import router as ai_media_router
from app.web.translate import router as translate_router


def create_app() -> FastAPI:
    manager = WorkspaceManager(Path(settings.data_dir), initial=settings.workspace)
    app_settings = AppSettings(Path(settings.data_dir), settings)
    embedder = build_embedder(app_settings.merged())
    core = CoreAPI(manager, settings, embedder, app_settings)
    core.queue = ArchiveQueue(core)
    jobs = JobManager(core)
    core.jobs = jobs

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs.start()
        yield
        await jobs.stop()

    app = FastAPI(title="DeepAggregator", lifespan=lifespan)
    app.state.core = core
    app.include_router(router)
    app.include_router(chats_router)
    app.include_router(ai_media_router)
    app.include_router(translate_router)
    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/", NoCacheStaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
