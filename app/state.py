"""Состояние лаборатории: ученики, очередь, назначения роботов, токены доступа."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .config import Robot, Settings

OwnerKind = Literal["student", "teacher"]


@dataclass
class StudentSession:
    """Заявка ученика на доступ к роботу."""

    id: str
    name: str
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    robot_id: str | None = None

    def is_online(self, timeout: float) -> bool:
        return (time.time() - self.last_seen) <= timeout


@dataclass
class PanelGrant:
    """Выданное право открыть панель конкретного робота через прокси."""

    token: str
    robot_id: str
    owner_kind: OwnerKind
    owner_id: str
    created_at: float = field(default_factory=time.time)


@dataclass
class RobotStatus:
    """Результат опроса робота фоновой задачей."""

    panel_online: bool = False
    ros_online: bool = False
    state_name: str | None = None
    state_id: int | None = None
    software_version: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    checked_at: float = 0.0

    @property
    def label(self) -> str:
        if not self.panel_online:
            return "Недоступен"
        if not self.ros_online:
            return "Панель есть, робот не отвечает"
        return self.state_name or "Активен"


class LabState:
    """Единое хранилище состояния лаборатории (в памяти процесса)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.robots: list[Robot] = settings.load_robots()
        self.students: dict[str, StudentSession] = {}
        self.status: dict[str, RobotStatus] = {robot.id: RobotStatus() for robot in self.robots}
        self.grants: dict[str, PanelGrant] = {}

    # --- роботы -------------------------------------------------------------

    def robot(self, robot_id: str) -> Robot | None:
        return next((item for item in self.robots if item.id == robot_id), None)

    @property
    def enabled_robots(self) -> list[Robot]:
        return [robot for robot in self.robots if robot.enabled]

    def replace_robots(self, robots: list[Robot]) -> None:
        self.robots = robots
        self.status = {robot.id: self.status.get(robot.id, RobotStatus()) for robot in robots}
        known = {robot.id for robot in robots}
        for student in self.students.values():
            if student.robot_id not in known:
                student.robot_id = None
        for token, grant in list(self.grants.items()):
            if grant.robot_id not in known:
                self.grants.pop(token, None)
        self.settings.save_robots(robots)

    # --- ученики ------------------------------------------------------------

    def add_student(self, name: str) -> StudentSession:
        session = StudentSession(id=secrets.token_urlsafe(24), name=name)
        self.students[session.id] = session
        return session

    def student(self, session_id: str | None) -> StudentSession | None:
        if not session_id:
            return None
        return self.students.get(session_id)

    def touch(self, session_id: str) -> None:
        student = self.students.get(session_id)
        if student:
            student.last_seen = time.time()

    def remove_student(self, session_id: str) -> None:
        self.students.pop(session_id, None)
        self._drop_grants(owner_id=session_id)

    def queue(self) -> list[StudentSession]:
        """Ожидающие подключения, в порядке подачи заявки."""
        waiting = [item for item in self.students.values() if item.robot_id is None]
        return sorted(waiting, key=lambda item: item.created_at)

    def connected_students(self) -> list[StudentSession]:
        return sorted(
            (item for item in self.students.values() if item.robot_id is not None),
            key=lambda item: item.created_at,
        )

    def student_on_robot(self, robot_id: str) -> StudentSession | None:
        return next(
            (item for item in self.students.values() if item.robot_id == robot_id),
            None,
        )

    def queue_position(self, session_id: str) -> int | None:
        queue = self.queue()
        for index, item in enumerate(queue, start=1):
            if item.id == session_id:
                return index
        return None

    # --- назначения и токены доступа ---------------------------------------

    def assign(self, robot_id: str, session_id: str) -> PanelGrant:
        """Подключить ученика к роботу, освободив прежние привязки."""
        robot = self.robot(robot_id)
        if robot is None:
            raise KeyError(f"Робот {robot_id} не найден")
        student = self.students.get(session_id)
        if student is None:
            raise KeyError("Заявка ученика не найдена")

        previous = self.student_on_robot(robot_id)
        if previous is not None and previous.id != session_id:
            self.disconnect_student(previous.id)
        self._drop_grants(owner_id=session_id)

        student.robot_id = robot_id
        grant = PanelGrant(
            token=secrets.token_urlsafe(24),
            robot_id=robot_id,
            owner_kind="student",
            owner_id=session_id,
        )
        self.grants[grant.token] = grant
        return grant

    def disconnect_student(self, session_id: str) -> None:
        student = self.students.get(session_id)
        if student is None:
            return
        student.robot_id = None
        self._drop_grants(owner_id=session_id)

    def free_robot(self, robot_id: str) -> None:
        student = self.student_on_robot(robot_id)
        if student is not None:
            self.disconnect_student(student.id)
        for token, grant in list(self.grants.items()):
            if grant.robot_id == robot_id and grant.owner_kind == "student":
                self.grants.pop(token, None)

    def teacher_grant(self, robot_id: str, teacher_id: str = "teacher") -> PanelGrant:
        robot = self.robot(robot_id)
        if robot is None:
            raise KeyError(f"Робот {robot_id} не найден")
        existing = next(
            (
                item
                for item in self.grants.values()
                if item.robot_id == robot_id and item.owner_kind == "teacher"
            ),
            None,
        )
        if existing is not None:
            return existing
        grant = PanelGrant(
            token=secrets.token_urlsafe(24),
            robot_id=robot_id,
            owner_kind="teacher",
            owner_id=teacher_id,
        )
        self.grants[grant.token] = grant
        return grant

    def grant(self, token: str | None) -> PanelGrant | None:
        if not token:
            return None
        return self.grants.get(token)

    def resolve_panel(self, token: str | None) -> tuple[PanelGrant, Robot] | None:
        grant = self.grant(token)
        if grant is None:
            return None
        robot = self.robot(grant.robot_id)
        if robot is None:
            return None
        return grant, robot

    def _drop_grants(self, owner_id: str) -> None:
        for token, grant in list(self.grants.items()):
            if grant.owner_id == owner_id:
                self.grants.pop(token, None)

    # --- обслуживание -------------------------------------------------------

    def cleanup(self) -> None:
        """Убрать заявки закрытых вкладок и просроченные сессии."""
        now = time.time()
        ttl = self.settings.session_ttl_minutes * 60
        offline_limit = self.settings.student_heartbeat_timeout * 4
        for session_id, student in list(self.students.items()):
            expired = now - student.created_at > ttl
            abandoned = student.robot_id is None and now - student.last_seen > offline_limit
            if expired or abandoned:
                self.remove_student(session_id)

    # --- представление для интерфейсов -------------------------------------

    def robot_view(self, robot: Robot, *, include_host: bool = False) -> dict[str, Any]:
        status = self.status.get(robot.id, RobotStatus())
        student = self.student_on_robot(robot.id)
        view: dict[str, Any] = {
            "id": robot.id,
            "name": robot.name,
            "enabled": robot.enabled,
            "panel_online": status.panel_online,
            "ros_online": status.ros_online,
            "status_label": status.label,
            "state_id": status.state_id,
            "software_version": status.software_version,
            "latency_ms": status.latency_ms,
            "error": status.error,
            "checked_at": status.checked_at,
            "student": {"id": student.id, "name": student.name} if student else None,
        }
        if include_host:
            view["host"] = robot.host
        return view
