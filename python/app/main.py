from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import load_config
from .services.acquisition import Acquisition


def create_app(acq: Acquisition | None = None) -> FastAPI:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format='{"t":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}')

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.acq = acq or Acquisition(load_config())
        await app.state.acq.start()
        yield
        await app.state.acq.stop()

    app = FastAPI(title="S7 PoC (Python)", version="1.0", lifespan=lifespan)
    # The dashboard (served from either :8001 or :8002) reads both APIs: allow GET from localhost origins only.
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
                       allow_methods=["GET"], allow_headers=[])
    app.include_router(router)
    dash = Path(os.environ.get("DASHBOARD_DIR", "/dashboard"))
    if dash.is_dir():
        app.mount("/static", StaticFiles(directory=dash), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(dash / "index.html")
    return app


app = create_app() if os.environ.get("S7POC_AUTOSTART", "1") == "1" else None
