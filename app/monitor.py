"""Фоновый опрос состояния роботов: доступность панели и ответ rosbridge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .config import Robot, Settings
from .rosbridge import (
    COBOT_STATES,
    GET_STATE_SERVICE,
    GET_STATE_TYPE,
    GET_VERSION_SERVICE,
    GET_VERSION_TYPE,
    RosBridgeClient,
    RosBridgeError,
)
from .state import LabState, RobotStatus

log = logging.getLogger(__name__)


class RobotMonitor:
    def __init__(self, state: LabState, settings: Settings) -> None:
        self.state = state
        self.settings = settings
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_all()
                self.state.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:  # опрос не должен ронять приложение
                log.exception("Ошибка опроса роботов")
            await asyncio.sleep(self.settings.status_poll_interval)

    async def poll_all(self) -> None:
        robots = list(self.state.robots)
        results = await asyncio.gather(
            *(self.poll(robot) for robot in robots), return_exceptions=True
        )
        for robot, result in zip(robots, results):
            if isinstance(result, RobotStatus):
                self.state.status[robot.id] = result
            else:
                self.state.status[robot.id] = RobotStatus(
                    error=str(result), checked_at=time.time()
                )

    async def poll(self, robot: Robot) -> RobotStatus:
        status = RobotStatus(checked_at=time.time())
        if not robot.enabled:
            status.error = "Робот отключён в настройках"
            return status

        started = time.perf_counter()
        status.panel_online = await _tcp_reachable(robot.host, robot.panel_port)
        status.latency_ms = int((time.perf_counter() - started) * 1000)
        if not status.panel_online:
            status.error = "Панель управления недоступна"
            return status

        try:
            async with RosBridgeClient(robot.ros_url, timeout=self.settings.ros_timeout) as client:
                state = await client.call_service(GET_STATE_SERVICE, GET_STATE_TYPE, {})
                status.ros_online = True
                state_id = (state.get("state") or {}).get("id")
                if isinstance(state_id, int):
                    status.state_id = state_id
                    status.state_name = COBOT_STATES.get(state_id, f"Состояние {state_id}")
                with contextlib.suppress(RosBridgeError):
                    version = await client.call_service(GET_VERSION_SERVICE, GET_VERSION_TYPE, {})
                    message = version.get("message")
                    if isinstance(message, str):
                        status.software_version = message
        except RosBridgeError as exc:
            status.error = str(exc)
        return status


async def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True
