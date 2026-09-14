"""Преобразование JSON-программ в формат, который понимает робот.

Панель Promobot собирает Blockly-программу в структуру
``{"Root": [{"Move": {"name": ..., "type": "Simple", "content": [...]}}]}``
и отправляет её сервисом /pm_api/save_program, после чего запускает
действием /pm_api/play_program.

Здесь поддержаны два входных формата:

* «родной» — объект уже содержит ключ ``Root``, он уходит на робота как есть;
* «простой» — ``{"blocks": [{"type": "delay", "time": 1}, ...]}``,
  который разворачивается в родной формат.
"""

from __future__ import annotations

import json
import time
from typing import Any

DEFAULT_PROGRAM_NAME = "labProgram"

# Панель робота исполняет код пользователя не как есть: она оборачивает его
# в шаблон, который поднимает SDK внутри контейнера робота и берёт управление.
# Без обёртки объекта manipulator в коде не существует.
PYTHON_TEMPLATE = """import asyncio
from sdk.commands.move_coordinates_command import (
    MoveCoordinatesParamsPosition,
    MoveCoordinatesParamsOrientation,
    PlannerType,
)
from sdk.commands.data import Point3D, Position, Orientation
from sdk.manipulators.{module} import {cls}

host: str = '{host}'
client_id: str = '{client_id}'
login: str = '{login}'
password: str = '{password}'

manipulator = {cls}(host, client_id, login, password)


async def _lab_program():
    try:
        manipulator.connect()
        manipulator.get_control()

{body}

    except Exception as error:
        print('Возникла ошибка')
        print(error)


loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
asyncio.get_event_loop().run_until_complete(_lab_program())
"""

DEVICE_SDK = {
    "medu": ("medu", "MEdu"),
    "m13": ("m13", "M13"),
}


def wrap_python_program(
    code: str,
    *,
    device: str = "medu",
    host: str = "172.17.0.1",
    client_id: str = "1227",
    login: str = "13",
    password: str = "14",
) -> str:
    """Обернуть код ученика в шаблон панели: SDK, подключение, обработка ошибок."""
    module, cls = DEVICE_SDK.get(device, DEVICE_SDK["medu"])
    body = "\n".join(
        f"        {line}" if line.strip() else "" for line in code.replace("\r\n", "\n").split("\n")
    )
    return PYTHON_TEMPLATE.format(
        module=module,
        cls=cls,
        host=host,
        client_id=client_id,
        login=login,
        password=password,
        body=body,
    )


class ProgramError(ValueError):
    """Ошибка разбора программы."""


def _number(block: dict[str, Any], key: str, default: float | None = None) -> float:
    value = block.get(key, default)
    if value is None:
        raise ProgramError(f"В блоке «{block.get('type')}» не хватает поля «{key}»")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProgramError(f"Поле «{key}» блока «{block.get('type')}» должно быть числом") from exc


def _positions(block: dict[str, Any]) -> list[float]:
    raw = block.get("positions")
    if not isinstance(raw, list) or not raw:
        raise ProgramError(f"Блок «{block.get('type')}» требует список «positions»")
    try:
        return [float(item) for item in raw]
    except (TypeError, ValueError) as exc:
        raise ProgramError("Значения «positions» должны быть числами") from exc


def _convert_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(block.get("type", "")).strip().lower()

    if kind in ("delay", "wait", "pause"):
        return [{"Delay": {"time": _number(block, "time", 1)}}]

    if kind in ("point", "move_to"):
        return [{"Point": {"positions": _positions(block), "time": _number(block, "time", 1)}}]

    if kind in ("move_joints", "joints"):
        velocity = _number(block, "velocity", 0.5)
        acceleration = _number(block, "acceleration", 0.5)
        commands: list[dict[str, Any]] = [
            {
                "MoveJoints": {
                    "positions": _positions(block),
                    "max_velocity_scaling_factor": velocity,
                    "max_acceleration_scaling_factor": acceleration,
                    "move_group": block.get("move_group", "manipulator"),
                    "planner": block.get("planner", "ompl"),
                    "planner_id": block.get("planner_id", "PTP"),
                    "name": block.get("name", ""),
                }
            }
        ]
        if block.get("delay"):
            commands.append({"Delay": {"time": _number(block, "delay")}})
        return commands

    if kind in ("move_cartesian", "coordinates", "move_coordinates"):
        return [
            {
                "MoveCartesianCoordinates": {
                    "position_x": _number(block, "x"),
                    "position_y": _number(block, "y"),
                    "position_z": _number(block, "z"),
                    "planner_id": block.get("planner_id", "PTP"),
                    "max_velocity_scaling_factor": _number(block, "velocity", 0.5),
                    "max_acceleration_scaling_factor": _number(block, "acceleration", 0.5),
                }
            }
        ]

    if kind == "audio":
        file_name = block.get("file") or block.get("audio_file")
        if not file_name:
            raise ProgramError("Блок «audio» требует поле «file»")
        return [
            {
                "Audio": {
                    "audio_file": file_name,
                    "run_in_background": bool(block.get("background", False)),
                }
            }
        ]

    if kind == "gpio":
        return [
            {
                "GPIOCommand": {
                    "gpio_name": block.get("gpio_name", ""),
                    "value": block.get("value"),
                    "name": block.get("name"),
                }
            }
        ]

    raise ProgramError(f"Неизвестный тип блока: «{block.get('type')}»")


def build_program_json(source: str) -> str:
    """Вернуть строку program_json для сервиса /pm_api/save_program."""
    text = (source or "").strip()
    if not text:
        raise ProgramError("Программа пуста")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProgramError(f"Некорректный JSON: {exc.msg} (строка {exc.lineno})") from exc

    if isinstance(data, dict) and "Root" in data:
        return json.dumps(data, ensure_ascii=False)

    blocks = data.get("blocks") if isinstance(data, dict) else data
    if not isinstance(blocks, list) or not blocks:
        raise ProgramError(
            "Ожидается объект с ключом «Root» или список блоков в поле «blocks»"
        )

    content: list[dict[str, Any]] = []
    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise ProgramError(f"Блок №{index} должен быть объектом")
        content.extend(_convert_block(block))

    program = {
        "Root": [
            {
                "Move": {
                    "content": content,
                    "name": f"Move_{int(time.time() * 1000)}",
                    "type": "Simple",
                }
            }
        ]
    }
    return json.dumps(program, ensure_ascii=False)
