"""Одновременный запуск кода на нескольких роботах."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .config import Robot, Settings
from .programs import (
    DEFAULT_PROGRAM_NAME,
    ProgramError,
    compile_run,
)
from .rosbridge import (
    EXECUTE_CODE_ACTION,
    EXECUTE_CODE_TYPE,
    PLAY_PROGRAM_ACTION,
    PLAY_PROGRAM_TYPE,
    SAVE_PROGRAM_SERVICE,
    SAVE_PROGRAM_TYPE,
    RosBridgeClient,
    RosBridgeError,
)
from .state import LabState

log = logging.getLogger(__name__)

RunKind = Literal["python", "program"]
RunStatus = Literal["running", "done", "error", "stopped"]

MAX_JOBS_KEPT = 30
MAX_FEEDBACK_LINES = 40


@dataclass
class RobotRun:
    """Результат выполнения на одном роботе."""

    robot_id: str
    robot_name: str
    status: RunStatus = "running"
    output: str = ""
    error: str = ""
    result_code: int | None = None
    feedback: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def view(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "robot_name": self.robot_name,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "result_code": self.result_code,
            "feedback": self.feedback[-6:],
            "duration": round((self.finished_at or time.time()) - self.started_at, 1),
        }


@dataclass
class RunJob:
    """Одна задача «запустить это на этих роботах»."""

    id: str
    kind: RunKind
    source: str
    created_at: float = field(default_factory=time.time)
    runs: dict[str, RobotRun] = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    @property
    def finished(self) -> bool:
        return all(run.status != "running" for run in self.runs.values())

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "created_at": self.created_at,
            "finished": self.finished,
            "preview": self.source[:160],
            "runs": [run.view() for run in self.runs.values()],
        }


class CodeRunner:
    def __init__(self, state: LabState, settings: Settings) -> None:
        self.state = state
        self.settings = settings
        self.jobs: dict[str, RunJob] = {}

    def job(self, job_id: str) -> RunJob | None:
        return self.jobs.get(job_id)

    def recent(self, limit: int = 5) -> list[RunJob]:
        ordered = sorted(self.jobs.values(), key=lambda item: item.created_at, reverse=True)
        return ordered[:limit]

    async def start(
        self,
        kind: RunKind,
        source: str,
        robot_ids: list[str],
        *,
        python_version: str = "",
        requirements: list[str] | None = None,
        wrap_sdk: bool = True,
    ) -> RunJob:
        robots: list[Robot] = []
        for robot_id in robot_ids:
            robot = self.state.robot(robot_id)
            if robot is None:
                raise KeyError(f"Робот {robot_id} не найден")
            robots.append(robot)
        if not robots:
            raise ValueError("Не выбран ни один робот")

        exec_kind, payload = compile_run(kind, source, wrap_sdk=wrap_sdk)

        job = RunJob(id=secrets.token_hex(6), kind=kind, source=source)
        job.runs = {
            robot.id: RobotRun(robot_id=robot.id, robot_name=robot.name) for robot in robots
        }
        self.jobs[job.id] = job
        self._trim()

        job.task = asyncio.create_task(
            self._run_all(
                job, robots, payload, python_version, requirements or [], exec_kind
            )
        )
        return job

    async def stop(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError("Задача не найдена")
        job.cancel.set()

    async def _run_all(
        self,
        job: RunJob,
        robots: list[Robot],
        payload: str,
        python_version: str,
        requirements: list[str],
        exec_kind: RunKind,
    ) -> None:
        await asyncio.gather(
            *(
                self._run_one(job, robot, payload, python_version, requirements, exec_kind)
                for robot in robots
            ),
            return_exceptions=True,
        )
        self._log_job(job)

    async def _run_one(
        self,
        job: RunJob,
        robot: Robot,
        payload: str,
        python_version: str,
        requirements: list[str],
        exec_kind: RunKind,
    ) -> None:
        run = job.runs[robot.id]

        def note(values: dict[str, Any]) -> None:
            message = values.get("message") or values.get("feedback") or json.dumps(
                values, ensure_ascii=False
            )
            text = str(message).strip()
            if text and (not run.feedback or run.feedback[-1] != text):
                run.feedback.append(text)
                del run.feedback[:-MAX_FEEDBACK_LINES]

        try:
            async with RosBridgeClient(robot.ros_url, timeout=self.settings.ros_timeout) as client:
                if exec_kind == "python":
                    values = await client.run_action(
                        EXECUTE_CODE_ACTION,
                        EXECUTE_CODE_TYPE,
                        {
                            "code": payload,
                            "python_version": python_version,
                            "requirements": requirements,
                        },
                        timeout=self.settings.code_timeout,
                        on_feedback=note,
                        cancel_event=job.cancel,
                    )
                else:
                    await client.call_service(
                        SAVE_PROGRAM_SERVICE,
                        SAVE_PROGRAM_TYPE,
                        {"program_name": DEFAULT_PROGRAM_NAME, "program_json": payload},
                    )
                    values = await client.run_action(
                        PLAY_PROGRAM_ACTION,
                        PLAY_PROGRAM_TYPE,
                        {"program_name": DEFAULT_PROGRAM_NAME},
                        timeout=self.settings.code_timeout,
                        on_feedback=note,
                        cancel_event=job.cancel,
                    )
        except RosBridgeError as exc:
            run.status = "stopped" if job.cancel.is_set() else "error"
            run.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - показываем учителю любую ошибку
            log.exception("Ошибка запуска на роботе %s", robot.id)
            run.status = "error"
            run.error = str(exc)
        else:
            code = values.get("result_code")
            run.result_code = code if isinstance(code, int) else None
            run.output = str(values.get("out") or "").strip()
            run.error = str(values.get("err") or "").strip()
            run.status = "error" if self._looks_failed(run) else "done"
        finally:
            run.finished_at = time.time()

    @staticmethod
    def _looks_failed(run: RobotRun) -> bool:
        """Робот отвечает result_code=0, даже если скрипт упал: смотрим на вывод."""
        if run.error or run.result_code not in (None, 0):
            return True
        markers = ("Traceback (most recent call last)", "Возникла ошибка")
        return any(marker in run.output for marker in markers)

    def _trim(self) -> None:
        if len(self.jobs) <= MAX_JOBS_KEPT:
            return
        ordered = sorted(self.jobs.values(), key=lambda item: item.created_at)
        for job in ordered[: len(self.jobs) - MAX_JOBS_KEPT]:
            if job.finished:
                self.jobs.pop(job.id, None)

    def _log_job(self, job: RunJob) -> None:
        try:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "job": job.id,
                "kind": job.kind,
                "robots": [
                    {"id": run.robot_id, "status": run.status, "error": run.error[:200]}
                    for run in job.runs.values()
                ],
                "source": job.source[:2000],
            }
            with self.settings.runs_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Не удалось записать журнал запусков: %s", exc)
