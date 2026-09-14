"""Панель управления учебной лабораторией Promobot M Edu."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import proxy, routes_admin, routes_public
from .config import settings
from .monitor import RobotMonitor
from .runner import CodeRunner
from .state import LabState

BASE_DIR = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
log = logging.getLogger("medu")


def _asset_version() -> str:
    """Метка для ?v= у css/js: браузер не отдаёт старые файлы после обновления."""
    newest = max(
        (path.stat().st_mtime for path in (BASE_DIR / "static").rglob("*") if path.is_file()),
        default=0.0,
    )
    return str(int(newest))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    lab = LabState(settings)
    app.state.lab = lab
    app.state.runner = CodeRunner(lab, settings)
    app.state.monitor = RobotMonitor(lab, settings)
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=6.0), follow_redirects=False
    )
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.globals["asset_version"] = _asset_version()
    app.state.templates = templates

    if settings.teacher_password == "1409":
        log.warning(
            "Используется пароль учителя по умолчанию — задайте TEACHER_PASSWORD в окружении."
        )
    log.info("Роботов в реестре: %s", len(lab.robots))

    await app.state.monitor.start()
    try:
        yield
    finally:
        await app.state.monitor.stop()
        await app.state.http.aclose()


app = FastAPI(
    title="Лаборатория Promobot M Edu",
    description="Управление доступом учеников к учебным роботам",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/health", include_in_schema=False)
async def health(request: Request):
    lab: LabState = request.app.state.lab
    return JSONResponse(
        {
            "status": "ok",
            "robots": len(lab.robots),
            "online": sum(1 for item in lab.status.values() if item.panel_online),
            "students": len(lab.students),
        }
    )


app.include_router(routes_public.router)
app.include_router(routes_admin.router)
# Прокси регистрируется последним: его резервный маршрут перехватывает
# корневые пути панели (/assets, /images, ...) и не должен мешать своим.
app.include_router(proxy.router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    wants_json = request.url.path.startswith(("/api/", "/admin/api/")) or "application/json" in (
        request.headers.get("accept") or ""
    )
    if wants_json:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "school_name": settings.school_name,
            "lab_name": settings.lab_name,
            "status_code": exc.status_code,
            "detail": exc.detail,
        },
        status_code=exc.status_code,
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return HTMLResponse(status_code=204)
