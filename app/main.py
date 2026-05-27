from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db
from .routes import router

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="Job Search Kit", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(router)
    return app


app = create_app()
