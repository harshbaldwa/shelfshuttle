import asyncio
import os
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import ROOT, api_router, router
from app.core.config import deployment_from_env, private_key
from app.core.security import LoginLimiter, RequestPolicy, session_secret
from app.core.settings import SettingsStore
from app.models.database import database
from app.services.qbittorrent import QBittorrent
from app.workers.worker import Worker


def create_app(deployment=None, *, overrides=None, worker_client=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        config = app.state.deployment
        key = private_key(config.data)
        engine, db = database(config.data)
        try:
            store = SettingsStore(config, key, overrides)
            with db() as session:
                store.get(session)
            app.state.runtime = SimpleNamespace(
                deployment=config,
                db=db,
                settings=store,
                login=LoginLimiter(),
                qbittorrent=worker_client or QBittorrent,
            )
            app.state.session_key = session_secret(key, config)
            stop = Event()
            worker = Worker(db, store, app.state.runtime.qbittorrent)
            app.state.worker = worker
            task = (
                asyncio.create_task(asyncio.to_thread(worker.run, stop))
                if config.worker_enabled
                else None
            )
            try:
                yield
            finally:
                stop.set()
                if task:
                    await task
                worker.close()
        finally:
            engine.dispose()

    app = FastAPI(
        title="ShelfShuttle",
        version="0.1.3",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Configuration is loaded at ASGI startup, including when using uvicorn directly.
    class StartupPolicy:
        def __init__(self, inner):
            self.inner = inner
            self.http = None

        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                self.http = None
                os.umask(0o077)
                app.state.deployment = (
                    deployment if deployment is not None else deployment_from_env()
                )
            if scope["type"] != "http":
                await self.inner(scope, receive, send)
                return
            if self.http is None:
                config = app.state.deployment
                self.http = RequestPolicy(
                    SessionMiddleware(
                        self.inner,
                        secret_key=app.state.session_key,
                        session_cookie="shelfshuttle_session",
                        max_age=12 * 60 * 60,
                        same_site="strict",
                        https_only=config.cookie_secure,
                    ),
                    upload_limit=config.upload_limit,
                )
            await self.http(scope, receive, send)

    app.add_middleware(StartupPolicy)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.include_router(router)
    app.include_router(api_router)
    return app


app = create_app()
