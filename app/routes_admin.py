"""Страницы и API учителя: очередь, назначения, статусы, запуск кода."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Robot, settings
from .programs import ProgramError
from .proxy import panel_url
from .rosbridge import STOP_MOVING_SERVICE, STOP_MOVING_TYPE, RosBridgeClient, RosBridgeError
from .runner import CodeRunner
from .security import (
    clear_teacher_cookie,
    is_teacher,
    issue_teacher_cookie,
    verify_teacher_password,
)
from .state import LabState

router = APIRouter(prefix="/admin")


def _lab(request: Request) -> LabState:
    return request.app.state.lab


def _runner(request: Request) -> CodeRunner:
    return request.app.state.runner


def require_teacher(request: Request) -> None:
    if not is_teacher(request):
        raise HTTPException(status_code=401, detail="Нужен вход учителя")


def _context(request: Request, **extra) -> dict:
    return {
        "request": request,
        "school_name": settings.school_name,
        "lab_name": settings.lab_name,
        "show_camera": True,
        **extra,
    }


# --- вход ------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_teacher(request):
        return RedirectResponse("/admin", status_code=303)
    return request.app.state.templates.TemplateResponse(
        "admin_login.html", _context(request, show_camera=False)
    )


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    if not verify_teacher_password(password):
        return request.app.state.templates.TemplateResponse(
            "admin_login.html",
            _context(request, error="Неверный пароль.", show_camera=False),
            status_code=401,
        )
    response = RedirectResponse("/admin", status_code=303)
    issue_teacher_cookie(response)
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    clear_teacher_cookie(response)
    return response


# --- страницы --------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not is_teacher(request):
        return RedirectResponse("/admin/login", status_code=303)
    return request.app.state.templates.TemplateResponse("admin.html", _context(request))


@router.get("/code", response_class=HTMLResponse)
async def code_page(request: Request):
    if not is_teacher(request):
        return RedirectResponse("/admin/login", status_code=303)
    return request.app.state.templates.TemplateResponse("admin_code.html", _context(request))


@router.get("/robots", response_class=HTMLResponse)
async def robots_page(request: Request):
    if not is_teacher(request):
        return RedirectResponse("/admin/login", status_code=303)
    lab = _lab(request)
    payload = [robot.model_dump() for robot in lab.robots]
    return request.app.state.templates.TemplateResponse(
        "admin_robots.html", _context(request, robots=payload)
    )


# --- состояние -------------------------------------------------------------


@router.get("/api/state", dependencies=[Depends(require_teacher)])
async def api_state(request: Request):
    lab = _lab(request)
    runner = _runner(request)
    teacher_tokens = {
        grant.robot_id: grant.token
        for grant in lab.grants.values()
        if grant.owner_kind == "teacher"
    }
    robots = []
    for robot in lab.robots:
        view = lab.robot_view(robot, include_host=True)
        token = teacher_tokens.get(robot.id)
        view["teacher_panel_url"] = panel_url(token) if token else None
        robots.append(view)

    return JSONResponse(
        {
            "now": time.time(),
            "robots": robots,
            "queue": [
                {
                    "id": student.id,
                    "name": student.name,
                    "waiting_for": int(time.time() - student.created_at),
                    "online": student.is_online(settings.student_heartbeat_timeout),
                }
                for student in lab.queue()
            ],
            "connected": [
                {
                    "id": student.id,
                    "name": student.name,
                    "robot_id": student.robot_id,
                    "online": student.is_online(settings.student_heartbeat_timeout),
                }
                for student in lab.connected_students()
            ],
            "jobs": [job.view() for job in runner.recent()],
        }
    )


class AssignPayload(BaseModel):
    robot_id: str
    student_id: str


@router.post("/api/assign", dependencies=[Depends(require_teacher)])
async def api_assign(request: Request, payload: AssignPayload):
    lab = _lab(request)
    try:
        grant = lab.assign(payload.robot_id, payload.student_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "panel_url": panel_url(grant.token)}


class RobotPayload(BaseModel):
    robot_id: str


@router.post("/api/free", dependencies=[Depends(require_teacher)])
async def api_free(request: Request, payload: RobotPayload):
    _lab(request).free_robot(payload.robot_id)
    return {"ok": True}


class StudentPayload(BaseModel):
    student_id: str


@router.post("/api/disconnect", dependencies=[Depends(require_teacher)])
async def api_disconnect(request: Request, payload: StudentPayload):
    _lab(request).disconnect_student(payload.student_id)
    return {"ok": True}


@router.post("/api/remove", dependencies=[Depends(require_teacher)])
async def api_remove(request: Request, payload: StudentPayload):
    _lab(request).remove_student(payload.student_id)
    return {"ok": True}


@router.post("/api/open", dependencies=[Depends(require_teacher)])
async def api_open(request: Request, payload: RobotPayload):
    lab = _lab(request)
    try:
        grant = lab.teacher_grant(payload.robot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "panel_url": panel_url(grant.token)}


@router.post("/api/stop-robot", dependencies=[Depends(require_teacher)])
async def api_stop_robot(request: Request, payload: RobotPayload):
    lab = _lab(request)
    robot = lab.robot(payload.robot_id)
    if robot is None:
        raise HTTPException(status_code=404, detail="Робот не найден")
    try:
        async with RosBridgeClient(robot.ros_url, timeout=settings.ros_timeout) as client:
            await client.call_service(STOP_MOVING_SERVICE, STOP_MOVING_TYPE, {})
    except RosBridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/api/refresh", dependencies=[Depends(require_teacher)])
async def api_refresh(request: Request):
    await request.app.state.monitor.poll_all()
    return {"ok": True}


# --- реестр роботов --------------------------------------------------------


class RobotsPayload(BaseModel):
    robots: list[Robot] = Field(min_length=1)


@router.post("/api/robots", dependencies=[Depends(require_teacher)])
async def api_save_robots(request: Request, payload: RobotsPayload):
    ids = [robot.id for robot in payload.robots]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Идентификаторы роботов должны быть уникальны")
    lab = _lab(request)
    lab.replace_robots(payload.robots)
    await request.app.state.monitor.poll_all()
    return {"ok": True, "count": len(payload.robots)}


# --- запуск кода -----------------------------------------------------------


class RunPayload(BaseModel):
    kind: str = "python"
    source: str
    robot_ids: list[str] = Field(min_length=1)
    python_version: str = ""
    requirements: list[str] = Field(default_factory=list)
    wrap_sdk: bool = True


@router.post("/api/run", dependencies=[Depends(require_teacher)])
async def api_run(request: Request, payload: RunPayload):
    if payload.kind not in ("python", "program"):
        raise HTTPException(status_code=400, detail="Тип запуска: python или program")
    runner = _runner(request)
    try:
        job = await runner.start(
            payload.kind,  # type: ignore[arg-type]
            payload.source,
            payload.robot_ids,
            python_version=payload.python_version,
            requirements=payload.requirements,
            wrap_sdk=payload.wrap_sdk,
        )
    except (ProgramError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "job": job.view()}


@router.get("/api/run/{job_id}", dependencies=[Depends(require_teacher)])
async def api_run_state(request: Request, job_id: str):
    job = _runner(request).job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job.view()


@router.post("/api/run/{job_id}/stop", dependencies=[Depends(require_teacher)])
async def api_run_stop(request: Request, job_id: str):
    try:
        await _runner(request).stop(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}
