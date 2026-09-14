"""Конфигурация приложения и реестр роботов."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ROBOTS: list[dict[str, Any]] = [
    {"id": "central-front", "name": "Центральный передний", "host": "192.168.103.192"},
    {"id": "central-back", "name": "Центральный задний", "host": "192.168.103.234"},
    {"id": "left-back", "name": "Левый задний", "host": "192.168.102.18"},
    {"id": "left-front", "name": "Левый передний", "host": "192.168.102.38"},
]


class Robot(BaseModel):
    """Один учебный робот Promobot M Edu."""

    id: str
    name: str
    host: str
    panel_port: int = 80
    ros_port: int = 9090
    rest_port: int = 8081
    enabled: bool = True

    @property
    def panel_base(self) -> str:
        return f"http://{self.host}:{self.panel_port}"

    @property
    def rest_base(self) -> str:
        return f"http://{self.host}:{self.rest_port}"

    @property
    def ros_url(self) -> str:
        return f"ws://{self.host}:{self.ros_port}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    school_name: str = "Школа №1409"
    lab_name: str = "Лаборатория Promobot M Edu"

    teacher_password: str = "1409"
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    data_dir: Path = Path("data")

    robots_json: str | None = None

    status_poll_interval: float = 12.0
    ros_timeout: float = 8.0
    code_timeout: float = 600.0

    session_ttl_minutes: int = 480
    student_heartbeat_timeout: float = 90.0
    max_name_length: int = 40

    camera_host: str = ""
    camera_user: str = ""
    camera_password: str = ""
    camera_port: int = 554
    camera_path: str = "/stream2"
    camera_rtsp_url: str = ""

    def load_robots(self) -> list[Robot]:
        """Реестр роботов: файл в data_dir → переменная окружения → значения по умолчанию."""
        stored = self.robots_file
        if stored.exists():
            try:
                raw = json.loads(stored.read_text("utf-8"))
                return [Robot(**item) for item in raw]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if self.robots_json:
            raw = json.loads(self.robots_json)
            return [Robot(**item) for item in raw]
        return [Robot(**item) for item in DEFAULT_ROBOTS]

    def save_robots(self, robots: list[Robot]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = [robot.model_dump() for robot in robots]
        self.robots_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    @property
    def robots_file(self) -> Path:
        return self.data_dir / "robots.json"

    @property
    def runs_log(self) -> Path:
        return self.data_dir / "runs.jsonl"

    @property
    def camera_configured(self) -> bool:
        return bool(self.camera_rtsp_url.strip() or self.camera_host.strip())


settings = Settings()
