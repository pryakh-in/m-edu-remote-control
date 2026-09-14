"""Реверс-прокси веб-панелей роботов.

Панель отдаётся ученику по адресу /p/<токен>/, поэтому браузер не видит
ни IP робота, ни его портов. Проксируются три канала:

* HTTP панели   -> http://<robot>:80
* rosbridge     -> ws://<robot>:9090   (через /p/<токен>/__ws/9090)
* REST панели   -> http://<robot>:8081 (через /p/<токен>/__rest/...)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from functools import lru_cache
from pathlib import Path

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .config import Robot
from .security import COOKIE_PANEL, COOKIE_STUDENT, is_teacher_cookies
from .state import LabState, PanelGrant

log = logging.getLogger(__name__)

router = APIRouter()

PANEL_PREFIX = "/p"
REST_SEGMENT = "__rest"
WS_SEGMENT = "__ws"

# Порты робота, к которым разрешено проксировать WebSocket.
ALLOWED_WS_PORTS = {9090, 8089, 8999}

# Пути панели, которые SPA запрашивает от корня сайта.
FALLBACK_PREFIXES = ("assets", "images", "configs", "locales", "fonts", "media", "audio")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
}

_ABSOLUTE_ATTR = re.compile(r'\b(src|href)="/(?!/)', re.IGNORECASE)
_STATIC_DIR = Path(__file__).parent / "static"


@lru_cache(maxsize=1)
def _bridge_script() -> str:
    return (_STATIC_DIR / "js" / "panel-bridge.js").read_text("utf-8")


def _lab(request: Request | WebSocket) -> LabState:
    return request.app.state.lab


def _http(request: Request | WebSocket) -> httpx.AsyncClient:
    return request.app.state.http


def authorize_panel(scope: Request | WebSocket, token: str) -> tuple[PanelGrant, Robot]:
    """Токен действителен только для того, кому он выдан (или для учителя)."""
    resolved = _lab(scope).resolve_panel(token)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Доступ к панели закрыт")
    grant, robot = resolved
    cookies = scope.cookies
    if is_teacher_cookies(cookies):
        return grant, robot
    if grant.owner_kind == "student" and cookies.get(COOKIE_STUDENT) == grant.owner_id:
        return grant, robot
    raise HTTPException(status_code=403, detail="Эта панель выдана другому пользователю")


def panel_url(token: str) -> str:
    return f"{PANEL_PREFIX}/{token}/"


def _prepare_headers(request: Request, robot: Robot) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP | {"host", "cookie", "referer", "origin"}
    }
    headers["host"] = f"{robot.host}:{robot.panel_port}"
    headers["accept-encoding"] = "identity"
    return headers


def _clean_response_headers(source: httpx.Response) -> dict[str, str]:
    return {
        key: value for key, value in source.headers.items() if key.lower() not in HOP_BY_HOP
    }


def _rewrite_html(body: str, base: str) -> str:
    """Переписать корневые пути и внедрить мост до запуска скриптов панели."""
    body = _ABSOLUTE_ATTR.sub(rf'\1="{base}/', body)
    injection = (
        f'<script>window.__PANEL_BASE__="{base}";</script>'
        f"<script>{_bridge_script()}</script>"
    )
    lowered = body.lower()
    head = lowered.find("<head")
    if head != -1:
        close = lowered.find(">", head)
        if close != -1:
            return body[: close + 1] + injection + body[close + 1 :]
    return injection + body


async def _proxy_request(
    request: Request,
    robot: Robot,
    upstream_url: str,
    base: str,
    *,
    set_panel_cookie: str | None = None,
) -> Response:
    client = _http(request)
    body = await request.body()
    upstream_request = client.build_request(
        request.method,
        upstream_url,
        headers=_prepare_headers(request, robot),
        params=dict(request.query_params),
        content=body or None,
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        log.warning("Панель %s недоступна: %s", robot.id, exc)
        raise HTTPException(status_code=502, detail="Панель робота недоступна") from exc

    content_type = upstream.headers.get("content-type", "")
    headers = _clean_response_headers(upstream)

    if "text/html" in content_type.lower():
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
        html = _rewrite_html(raw.decode("utf-8", errors="replace"), base)
        headers["cache-control"] = "no-store"
        response: Response = Response(
            content=html,
            status_code=upstream.status_code,
            headers=headers,
            media_type=content_type or "text/html; charset=utf-8",
        )
    else:
        response = StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=headers,
            media_type=content_type or None,
            background=BackgroundTask(upstream.aclose),
        )

    if set_panel_cookie:
        response.set_cookie(
            COOKIE_PANEL, set_panel_cookie, httponly=True, samesite="lax", path="/"
        )
    return response


@router.api_route(
    "/p/{token}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_panel(token: str, path: str, request: Request) -> Response:
    grant, robot = authorize_panel(request, token)
    base = f"{PANEL_PREFIX}/{token}"

    if path == REST_SEGMENT or path.startswith(f"{REST_SEGMENT}/"):
        tail = path[len(REST_SEGMENT) :].lstrip("/")
        return await _proxy_request(request, robot, f"{robot.rest_base}/{tail}", base)

    return await _proxy_request(
        request,
        robot,
        f"{robot.panel_base}/{path}",
        base,
        set_panel_cookie=grant.token if path in ("", "index.html") else None,
    )


@router.websocket("/p/{token}/__ws/{port}")
async def proxy_panel_ws(websocket: WebSocket, token: str, port: int) -> None:
    try:
        _, robot = authorize_panel(websocket, token)
    except HTTPException:
        await websocket.close(code=4403)
        return
    if port not in ALLOWED_WS_PORTS | {robot.ros_port}:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    upstream_url = f"ws://{robot.host}:{port}/"
    try:
        upstream = await websockets.connect(
            upstream_url, max_size=None, open_timeout=10, ping_interval=20
        )
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
        log.warning("rosbridge %s:%s недоступен: %s", robot.id, port, exc)
        await websocket.close(code=1011)
        return

    async def client_to_robot() -> None:
        while True:
            message = await websocket.receive()
            kind = message.get("type")
            if kind == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def robot_to_client() -> None:
        async for payload in upstream:
            if isinstance(payload, bytes):
                await websocket.send_bytes(payload)
            else:
                await websocket.send_text(payload)

    tasks = [asyncio.create_task(client_to_robot()), asyncio.create_task(robot_to_client())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(
                WebSocketDisconnect, websockets.WebSocketException, OSError, asyncio.CancelledError
            ):
                task.result()
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()


@router.api_route(
    "/{prefix}/{path:path}", methods=["GET", "HEAD"], include_in_schema=False
)
async def proxy_panel_fallback(prefix: str, path: str, request: Request) -> Response:
    """Панель иногда запрашивает файлы от корня сайта — находим робота по куке."""
    if prefix not in FALLBACK_PREFIXES:
        raise HTTPException(status_code=404, detail="Не найдено")
    token = request.cookies.get(COOKIE_PANEL)
    if not token:
        raise HTTPException(status_code=404, detail="Не найдено")
    try:
        _, robot = authorize_panel(request, token)
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail="Не найдено") from exc
    return await _proxy_request(
        request, robot, f"{robot.panel_base}/{prefix}/{path}", f"{PANEL_PREFIX}/{token}"
    )
