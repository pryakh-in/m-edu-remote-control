"""Страницы и API для учеников: заявка, ожидание, панель робота."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import settings
from .proxy import panel_url
from .security import clear_student_cookie, set_student_cookie, student_session_id
from .state import LabState

router = APIRouter()


def _lab(request: Request) -> LabState:
    return request.app.state.lab


def _templates(request: Request):
    return request.app.state.templates


def _context(request: Request, **extra) -> dict:
    return {
        "request": request,
        "school_name": settings.school_name,
        "lab_name": settings.lab_name,
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    lab = _lab(request)
    student = lab.student(student_session_id(request))
    if student is not None:
        return RedirectResponse("/wait", status_code=303)
    return _templates(request).TemplateResponse("index.html", _context(request))


@router.post("/join")
async def join(request: Request, name: str = Form(...)):
    lab = _lab(request)
    clean = " ".join(name.split())[: settings.max_name_length]
    if len(clean) < 2:
        return _templates(request).TemplateResponse(
            "index.html",
            _context(request, error="Введите имя и фамилию — так учитель вас узнает."),
            status_code=400,
        )

    existing = lab.student(student_session_id(request))
    if existing is not None:
        existing.name = clean
        lab.touch(existing.id)
        return RedirectResponse("/wait", status_code=303)

    student = lab.add_student(clean)
    response = RedirectResponse("/wait", status_code=303)
    set_student_cookie(response, student.id)
    return response


@router.get("/wait", response_class=HTMLResponse)
async def wait(request: Request):
    lab = _lab(request)
    student = lab.student(student_session_id(request))
    if student is None:
        return RedirectResponse("/", status_code=303)
    lab.touch(student.id)
    return _templates(request).TemplateResponse(
        "wait.html", _context(request, student=student)
    )


@router.get("/api/me")
async def api_me(request: Request):
    lab = _lab(request)
    session_id = student_session_id(request)
    student = lab.student(session_id)
    if student is None:
        return JSONResponse({"state": "unknown"}, status_code=404)

    lab.touch(student.id)
    if student.robot_id is None:
        return JSONResponse(
            {
                "state": "waiting",
                "name": student.name,
                "position": lab.queue_position(student.id),
                "queue_total": len(lab.queue()),
                "robots_free": sum(
                    1
                    for robot in lab.enabled_robots
                    if lab.student_on_robot(robot.id) is None
                ),
            }
        )

    robot = lab.robot(student.robot_id)
    grant = next(
        (
            item
            for item in lab.grants.values()
            if item.owner_id == student.id and item.robot_id == student.robot_id
        ),
        None,
    )
    if robot is None or grant is None:
        lab.disconnect_student(student.id)
        return JSONResponse({"state": "waiting", "name": student.name, "position": None})

    status = lab.status.get(robot.id)
    return JSONResponse(
        {
            "state": "connected",
            "name": student.name,
            "robot_name": robot.name,
            "robot_status": status.label if status else None,
            "panel_url": panel_url(grant.token),
        }
    )


@router.post("/api/leave")
async def api_leave(request: Request):
    lab = _lab(request)
    session_id = student_session_id(request)
    if session_id:
        lab.remove_student(session_id)
    response = JSONResponse({"ok": True})
    clear_student_cookie(response)
    return response
