"""Преобразование программ в то, что робот умеет исполнять.

Панель Promobot из Blockly не собирает «Root»-JSON: она генерирует Python
и запускает его действием ``/pm_api/execute_code``. Экспорт с панели —
это ``{"jointPoints": ..., "cpiPoints": ..., "program": {"blocks": ...}}``.

Поддерживаются:

* экспорт Blockly с панели (точки + workspace JSON) → Python SDK;
* «родной» ``{"Root": [...]}`` → save_program / play_program;
* простой ``{"blocks": [{"type": "delay", ...}, ...]}`` → Root JSON.
"""

from __future__ import annotations

import json
import math
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


def _parse_json(source: str) -> Any:
    text = (source or "").strip()
    if not text:
        raise ProgramError("Программа пуста")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProgramError(f"Некорректный JSON: {exc.msg} (строка {exc.lineno})") from exc


def is_blockly_export(data: Any) -> bool:
    """Экспорт «Скачать» из Blockly-панели робота, а не наш упрощённый список блоков."""
    if not isinstance(data, dict):
        return False
    if "jointPoints" in data or "cpiPoints" in data:
        return True
    program = data.get("program")
    if isinstance(program, dict) and isinstance(program.get("blocks"), dict):
        return True
    blocks = data.get("blocks")
    return isinstance(blocks, dict) and (
        "languageVersion" in blocks or isinstance(blocks.get("blocks"), list)
    )


def _points_map(raw: Any) -> dict[int, dict[str, Any]]:
    """Vue/JS Map в JSON бывает списком пар ``[id, point]`` или объектом."""
    result: dict[int, dict[str, Any]] = {}
    if raw is None:
        return result
    if isinstance(raw, dict):
        items: list[Any] = list(raw.items())
    elif isinstance(raw, list):
        items = raw
    else:
        return result
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            key, value = item
        elif isinstance(item, dict) and "id" in item:
            key, value = item["id"], item
        else:
            continue
        if not isinstance(value, dict):
            continue
        try:
            result[int(key)] = value
        except (TypeError, ValueError):
            continue
    return result


def _workspace_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    program = data.get("program") if isinstance(data.get("program"), dict) else data
    inner = program.get("blocks") if isinstance(program, dict) else None
    if isinstance(inner, dict):
        blocks = inner.get("blocks") or []
    elif isinstance(inner, list):
        blocks = inner
    else:
        blocks = []
    if not isinstance(blocks, list) or not blocks:
        raise ProgramError("В файле Blockly нет блоков программы")
    return [block for block in blocks if isinstance(block, dict)]


def _field(block: dict[str, Any], name: str, default: Any = None) -> Any:
    fields = block.get("fields")
    if isinstance(fields, dict) and name in fields:
        return fields[name]
    return default


def _input_child(block: dict[str, Any], name: str) -> dict[str, Any] | None:
    inputs = block.get("inputs")
    if not isinstance(inputs, dict):
        return None
    slot = inputs.get(name)
    if not isinstance(slot, dict):
        return None
    child = slot.get("block") or slot.get("shadow")
    return child if isinstance(child, dict) else None


def _next_block(block: dict[str, Any]) -> dict[str, Any] | None:
    nxt = block.get("next")
    if not isinstance(nxt, dict):
        return None
    child = nxt.get("block")
    return child if isinstance(child, dict) else None


def _numeric_value(block: dict[str, Any] | None, default: float | None = None) -> float | None:
    if block is None:
        return default
    kind = str(block.get("type") or "")
    if kind == "math_number":
        try:
            return float(_field(block, "NUM", default))
        except (TypeError, ValueError):
            return default
    if kind == "math_round":
        value = _numeric_value(_input_child(block, "NUM"), default)
        op = str(_field(block, "OP", "ROUND")).upper()
        if value is None:
            return default
        if op == "ROUNDUP":
            return math.ceil(value)
        if op == "ROUNDDOWN":
            return math.floor(value)
        return float(round(value))
    if kind == "math_arithmetic":
        left = _numeric_value(_input_child(block, "A"), 0) or 0
        right = _numeric_value(_input_child(block, "B"), 0) or 0
        op = str(_field(block, "OP", "ADD")).upper()
        if op == "MINUS":
            return left - right
        if op == "MULTIPLY":
            return left * right
        if op == "DIVIDE":
            return left / right if right else default
        if op == "POWER":
            return left**right
        return left + right
    return default


def _int_input(block: dict[str, Any], name: str, default: int) -> int:
    value = _numeric_value(_input_child(block, name))
    if value is None:
        return default
    return int(value)


def _fmt_num(value: float) -> str:
    text = f"{float(value):.12g}"
    return text if text else "0"


def _degrees_csv_to_radians(raw: str) -> str:
    parts = [item.strip() for item in str(raw).split(",") if item.strip()]
    if not parts:
        raise ProgramError("У точки суставов пустой список положений")
    radians = [math.radians(float(item)) for item in parts]
    return ", ".join(_fmt_num(item) for item in radians)


def _get_point(
    point_id: str, joints: dict[int, dict[str, Any]], cpis: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    key = str(point_id or "")
    try:
        if key.startswith("j"):
            return joints.get(int(key[1:]))
        if key.startswith("c"):
            return cpis.get(int(key[1:]))
        index = int(key)
    except ValueError:
        return None
    return joints.get(index) or cpis.get(index)


def _emit_move_to_point(
    block: dict[str, Any],
    joints: dict[int, dict[str, Any]],
    cpis: dict[int, dict[str, Any]],
) -> str:
    extra = block.get("extraState") if isinstance(block.get("extraState"), dict) else {}
    point_id = str(extra.get("selectedPointId") or _field(block, "pointId") or "")
    velocity = _fmt_num(float(_field(block, "velocity", 1) or 1))
    acceleration = _fmt_num(float(_field(block, "acceleration", 1) or 1))
    planner = str(_field(block, "plannerType") or "PlannerType.PTP")
    planner_id = "LIN" if "LIN" in planner else "PTP"
    point = _get_point(point_id, joints, cpis)

    if point and point.get("positions"):
        angles = _degrees_csv_to_radians(str(point["positions"]))
        return (
            f"manipulator.move_to_angles({angles}, velocity_factor={velocity}, "
            f"acceleration_factor={acceleration}, planner_id=\"{planner_id}\")"
        )
    if point and isinstance(point.get("joints"), list):
        angles = ", ".join(
            _fmt_num(math.radians(float(item.get("positionInDegrees", 0))))
            for item in point["joints"]
            if isinstance(item, dict)
        )
        return (
            f"manipulator.move_to_angles({angles}, velocity_factor={velocity}, "
            f"acceleration_factor={acceleration}, planner_id=\"{planner_id}\")"
        )
    if point_id in ("j0", "0") and not point:
        return (
            "manipulator.move_to_angles(manipulator.get_home_position(), "
            f"velocity_factor={velocity}, acceleration_factor={acceleration}, "
            f'planner_id="{planner_id}")'
        )
    coords = point.get("coordinates") if point else None
    if isinstance(coords, dict):
        px, py, pz = coords.get("x", 0), coords.get("y", 0), coords.get("z", 0)
        orientation = point.get("orientation") if point else None
        if isinstance(orientation, dict):
            ox = orientation.get("x", 0)
            oy = orientation.get("y", 0)
            oz = orientation.get("z", 0)
            ow = orientation.get("w", 1)
        else:
            ox = oy = oz = 0
            ow = 1
        return (
            "manipulator.move_to_coordinates("
            f"position=MoveCoordinatesParamsPosition({_fmt_num(float(px))}, "
            f"{_fmt_num(float(py))}, {_fmt_num(float(pz))}), "
            f"orientation=MoveCoordinatesParamsOrientation("
            f"{_fmt_num(float(ox))}, {_fmt_num(float(oy))}, "
            f"{_fmt_num(float(oz))}, {_fmt_num(float(ow))}), "
            f"velocity_scaling_factor={velocity}, "
            f"acceleration_scaling_factor={acceleration}, "
            f"planner_type={planner})"
        )
    raise ProgramError(
        f"Блок «переместиться в точку» ссылается на неизвестную точку «{point_id}»"
    )


def _emit_blockly(
    block: dict[str, Any] | None,
    joints: dict[int, dict[str, Any]],
    cpis: dict[int, dict[str, Any]],
    indent: int,
) -> list[str]:
    lines: list[str] = []
    pad = "    " * indent
    current = block
    while current:
        if current.get("enabled") is False or current.get("disabled") is True:
            current = _next_block(current)
            continue
        kind = str(current.get("type") or "")
        if kind in ("controls_repeat_ext", "controls_repeat", "for_loop"):
            times = _int_input(current, "TIMES", int(float(_field(current, "TIMES", 1) or 1)))
            body = _input_child(current, "DO") or _input_child(current, "actions")
            lines.append(f"{pad}for _ in range({max(times, 0)}):")
            inner = _emit_blockly(body, joints, cpis, indent + 1)
            lines.extend(inner or [f"{pad}    pass"])
        elif kind in ("controls_whileUntil", "while_loop"):
            mode = str(_field(current, "MODE", "WHILE")).upper()
            body = _input_child(current, "DO") or _input_child(current, "actions")
            lines.append(f"{pad}while True:  # условие Blockly упрощено ({mode})")
            inner = _emit_blockly(body, joints, cpis, indent + 1)
            lines.extend(inner or [f"{pad}    pass"])
        elif kind == "move_to_point":
            lines.append(pad + _emit_move_to_point(current, joints, cpis))
        elif kind == "wait":
            delay = float(_field(current, "time", 1) or 1)
            lines.append(f"{pad}await asyncio.sleep({_fmt_num(delay)})")
        elif kind in ("play_audio", "play_audio_block"):
            name = str(_field(current, "audioFile") or "")
            lines.append(f"{pad}manipulator.play_audio({name!r})")
        elif kind == "mechanical_gripper_grip":
            lines.append(f"{pad}manipulator.manage_gripper(gripper={_field(current, 'gripId', 0)})")
        elif kind == "mechanical_gripper_rotation":
            lines.append(
                f"{pad}manipulator.manage_gripper(rotation={_field(current, 'rotationId', 0)})"
            )
        elif kind == "vacuum_gripper_state":
            lines.append(
                f"{pad}manipulator.manage_vacuum(power_supply={_field(current, 'stateId', 0)})"
            )
        elif kind == "vacuum_gripper_rotation":
            lines.append(
                f"{pad}manipulator.manage_vacuum(rotation={_field(current, 'rotationId', 0)})"
            )
        elif kind in ("text", "text_print"):
            current = _next_block(current)
            continue
        else:
            raise ProgramError(
                f"Блок Blockly «{kind or 'без типа'}» пока не умеем исполнять из файла. "
                "Откройте программу на панели робота или упростите её."
            )
        current = _next_block(current)
    return lines


def blockly_export_to_python(data: dict[str, Any]) -> str:
    """Собрать Python SDK-код из JSON, который панель сохраняет кнопкой «скачать»."""
    joints = _points_map(data.get("jointPoints"))
    cpis = _points_map(data.get("cpiPoints"))
    roots = _workspace_blocks(data)
    lines: list[str] = []
    for root in roots:
        lines.extend(_emit_blockly(root, joints, cpis, 0))
    code = "\n".join(lines).strip()
    if not code:
        raise ProgramError("Из Blockly-файла не получилось собрать ни одной команды")
    return code + "\n"


def compile_run(kind: str, source: str, *, wrap_sdk: bool = True) -> tuple[str, str]:
    """Вернуть (как исполнять, полезную нагрузку): python-код или Root JSON."""
    if kind == "python":
        if not (source or "").strip():
            raise ProgramError("Код пуст")
        return "python", wrap_python_program(source) if wrap_sdk else source

    data = _parse_json(source)
    if is_blockly_export(data):
        code = blockly_export_to_python(data)
        return "python", wrap_python_program(code)

    return "program", build_program_json(source)


def build_program_json(source: str) -> str:
    """Вернуть строку program_json для сервиса /pm_api/save_program."""
    data = _parse_json(source)

    if isinstance(data, dict) and "Root" in data:
        return json.dumps(data, ensure_ascii=False)

    blocks = data.get("blocks") if isinstance(data, dict) else data
    if not isinstance(blocks, list) or not blocks:
        raise ProgramError(
            "Ожидается объект с ключом «Root», экспорт Blockly с панели "
            "или список блоков в поле «blocks»"
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
