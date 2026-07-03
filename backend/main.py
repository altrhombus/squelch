import logging
import os
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import context
from . import db as dbmod
from .db import close_db, init_db
from .icecast import IcecastPusher
from .metadata import ART_DIR, MetadataState
from .radio.manager import RadioManager
from .recorder import Recorder
from .stations import seed_default_presets
from .streaming import StreamingManager
from .routes import history, presets, recordings, stream, tuner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH  = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
_EXAMPLE_PATH = _CONFIG_PATH + ".example"
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _load_config() -> dict:
    path = _CONFIG_PATH if os.path.exists(_CONFIG_PATH) else _EXAMPLE_PATH
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    config = _load_config()
    context.config = config

    db_path = (config.get("database") or {}).get("path")
    if db_path:
        dbmod.DB_PATH = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(dbmod.DB_PATH), exist_ok=True)
    await init_db()

    os.makedirs(ART_DIR, exist_ok=True)

    context.meta     = MetadataState()
    context.streams  = StreamingManager()
    context.radio    = RadioManager(config, context.meta, context.streams)
    context.recorder = Recorder(config, context.meta)
    context.recorder.set_streaming(context.streams)
    context.recorder.set_radio(context.radio)

    await context.radio.startup()
    await context.recorder.startup()

    ice_cfg = config.get("icecast") or {}
    if ice_cfg.get("enabled"):
        context.icecast = IcecastPusher(ice_cfg, context.meta, context.streams)
        context.icecast.start()

    defaults = config.get("default_presets", [])
    if defaults:
        await seed_default_presets(defaults)

    yield

    if context.icecast:
        await context.icecast.stop()
        context.icecast = None
    await context.radio.stop()
    await context.recorder.shutdown()
    await close_db()


app = FastAPI(title="Squelch", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Static files + root
# ---------------------------------------------------------------------------

os.makedirs(ART_DIR, exist_ok=True)
app.mount("/art",    StaticFiles(directory=ART_DIR),       name="art")
app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

app.include_router(stream.router)
app.include_router(tuner.router)
app.include_router(presets.router)
app.include_router(recordings.router)
app.include_router(history.router)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    srv = _load_config().get("server", {})
    uvicorn.run("backend.main:app", host=srv.get("host", "0.0.0.0"),
                port=srv.get("port", 8000), reload=False)
