"""Асинхронный клиент rosbridge для Promobot M Edu.

Панель робота общается с ним по протоколу rosbridge на ws://<ip>:9090.
Помимо стандартных операций (call_service, subscribe) прошивка использует
расширение для ROS 2 actions: send_action_goal / action_feedback /
action_result / cancel_action_goal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger(__name__)

FeedbackHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

EXECUTE_CODE_ACTION = "/pm_api/execute_code"
EXECUTE_CODE_TYPE = "pm_api_msgs/action/ExecuteCode"
PLAY_PROGRAM_ACTION = "/pm_api/play_program"
PLAY_PROGRAM_TYPE = "pm_api_msgs/action/PlayProgram"
SAVE_PROGRAM_SERVICE = "/pm_api/save_program"
SAVE_PROGRAM_TYPE = "pm_api_msgs/srv/SaveProgram"
GET_STATE_SERVICE = "/pm_api/get_cobot_state"
GET_STATE_TYPE = "pm_api_msgs/srv/GetCobotState"
GET_VERSION_SERVICE = "/pm_api/get_version_software"
GET_VERSION_TYPE = "pm_api_msgs/srv/GetVersionSoftware"
STOP_MOVING_SERVICE = "/pm_api/stop_moving"
STOP_MOVING_TYPE = "std_srvs/srv/Trigger"

# Значения pm_api_msgs CobotState из прошивки панели управления.
COBOT_STATES = {
    0: "Неизвестно",
    1: "Не настроен",
    2: "Неактивен",
    3: "Активен",
    4: "Завершён",
    10: "Настройка",
    11: "Очистка",
    12: "Выключение",
    13: "Активация",
    14: "Деактивация",
    15: "Обработка ошибки",
}


class RosBridgeError(RuntimeError):
    """Ошибка обмена с rosbridge."""


def _new_id() -> str:
    return f"{random.randint(100000, 999999)}"


class RosBridgeClient:
    """Одно соединение с rosbridge робота с мультиплексированием ответов по id."""

    def __init__(self, url: str, timeout: float = 8.0) -> None:
        self.url = url
        self.timeout = timeout
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._waiters: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._closed = asyncio.Event()

    async def __aenter__(self) -> RosBridgeClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.url, max_size=32 * 1024 * 1024, open_timeout=self.timeout),
                timeout=self.timeout,
            )
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            raise RosBridgeError(f"Не удалось подключиться к {self.url}: {exc}") from exc
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._closed.set()
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                key = message.get("id") or message.get("topic")
                if key and key in self._waiters:
                    await self._waiters[key].put(message)
        except (websockets.WebSocketException, OSError) as exc:
            log.debug("rosbridge %s: соединение закрыто (%s)", self.url, exc)
        finally:
            self._closed.set()
            for queue in self._waiters.values():
                await queue.put({"op": "__closed__"})

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RosBridgeError("Соединение с rosbridge не установлено")
        try:
            await self._ws.send(json.dumps(payload))
        except (websockets.WebSocketException, OSError) as exc:
            raise RosBridgeError(f"Не удалось отправить запрос: {exc}") from exc

    @contextlib.contextmanager
    def _channel(self, key: str):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._waiters[key] = queue
        try:
            yield queue
        finally:
            self._waiters.pop(key, None)

    async def call_service(
        self,
        name: str,
        service_type: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        call_id = _new_id()
        with self._channel(call_id) as queue:
            await self._send(
                {
                    "op": "call_service",
                    "id": call_id,
                    "service": name,
                    "type": service_type,
                    "args": args or {},
                }
            )
            message = await self._expect(queue, timeout or self.timeout, name)
        if message.get("result") is False:
            raise RosBridgeError(f"Сервис {name} вернул ошибку: {message.get('values')}")
        return message.get("values") or {}

    async def run_action(
        self,
        action: str,
        action_type: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
        on_feedback: FeedbackHandler | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Отправить цель action и дождаться результата, прокидывая feedback."""
        goal_id = _new_id()
        with self._channel(goal_id) as queue:
            await self._send(
                {
                    "op": "send_action_goal",
                    "id": goal_id,
                    "action": action,
                    "action_type": action_type,
                    "args": args or {},
                    "feedback": True,
                }
            )
            deadline = asyncio.get_running_loop().time() + (timeout or self.timeout)
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    await self.cancel_action(action, goal_id)
                    raise RosBridgeError("Выполнение остановлено")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self.cancel_action(action, goal_id)
                    raise RosBridgeError(f"Таймаут выполнения {action}")
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    continue
                op = message.get("op")
                if op == "__closed__":
                    raise RosBridgeError("Соединение с роботом потеряно")
                if op == "action_feedback":
                    if on_feedback is not None:
                        result = on_feedback(message.get("values") or {})
                        if asyncio.iscoroutine(result):
                            await result
                    continue
                if op == "action_result":
                    values = message.get("values") or {}
                    if message.get("result") is False:
                        raise RosBridgeError(str(values) or "Действие завершилось с ошибкой")
                    return values

    async def cancel_action(self, action: str, goal_id: str) -> None:
        with contextlib.suppress(RosBridgeError):
            await self._send({"op": "cancel_action_goal", "id": goal_id, "action": action})

    async def _expect(
        self, queue: asyncio.Queue[dict[str, Any]], timeout: float, what: str
    ) -> dict[str, Any]:
        try:
            message = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RosBridgeError(f"Таймаут ожидания ответа от {what}") from exc
        if message.get("op") == "__closed__":
            raise RosBridgeError("Соединение с роботом потеряно")
        return message
