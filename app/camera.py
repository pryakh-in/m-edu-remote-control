"""Прокси камеры Tapo: RTSP по TCP → MJPEG по HTTP.

Браузер не умеет RTSP. ffmpeg забирает поток с камеры по TCP
(``-rtsp_transport tcp``, без UDP) и отдаёт multipart JPEG. Клиенты видят
только ``/camera/mjpeg`` — адрес и пароль камеры наружу не уходят.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, settings
from .security import is_teacher, student_session_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/camera", tags=["camera"])

FFMPEG_BOUNDARY = "ffmpeg"


class CameraRelay:
    """Один процесс ffmpeg на всех зрителей: RTSP/TCP → MJPEG."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task[None] | None = None
        self._stop_idle: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._lock = asyncio.Lock()
        self._last_error = ""

    @property
    def configured(self) -> bool:
        return self.cfg.camera_configured

    def rtsp_url(self) -> str:
        if self.cfg.camera_rtsp_url.strip():
            return self.cfg.camera_rtsp_url.strip()
        user = quote(self.cfg.camera_user, safe="")
        password = quote(self.cfg.camera_password, safe="")
        host = self.cfg.camera_host.strip()
        port = self.cfg.camera_port
        path = self.cfg.camera_path.strip() or "/stream2"
        if not path.startswith("/"):
            path = "/" + path
        auth = f"{user}:{password}@" if user or password else ""
        return f"rtsp://{auth}{host}:{port}{path}"

    def ffmpeg_available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.configured,
            "ffmpeg": self.ffmpeg_available(),
            "live": self._proc is not None and self._proc.returncode is None,
            "viewers": len(self._subscribers),
            "error": self._last_error,
            "host": self.cfg.camera_host or None,
        }

    async def subscribe(self) -> asyncio.Queue[bytes | None]:
        if not self.configured:
            raise RuntimeError("Камера не настроена")
        if not self.ffmpeg_available():
            raise RuntimeError("В контейнере нет ffmpeg")
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=12)
        async with self._lock:
            if self._stop_idle is not None:
                self._stop_idle.cancel()
                self._stop_idle = None
            self._subscribers.add(queue)
            await self._ensure_ffmpeg()
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
            if not self._subscribers:
                self._stop_idle = asyncio.create_task(self._idle_stop())

    async def stop(self) -> None:
        async with self._lock:
            await self._kill_ffmpeg()

    async def _idle_stop(self) -> None:
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if not self._subscribers:
                await self._kill_ffmpeg()

    async def _ensure_ffmpeg(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        await self._spawn_ffmpeg()

    async def _spawn_ffmpeg(self) -> None:
        await self._kill_ffmpeg()
        url = self.rtsp_url()
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-timeout",
            "5000000",
            "-i",
            url,
            "-an",
            "-vf",
            "scale=640:-2",
            "-q:v",
            "7",
            "-r",
            "8",
            "-f",
            "mpjpeg",
            "-",
        ]
        log.info(
            "Камера: ffmpeg RTSP/TCP %s:%s%s",
            self.cfg.camera_host or "url",
            self.cfg.camera_port,
            self.cfg.camera_path,
        )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self._last_error = "ffmpeg не найден"
            raise RuntimeError(self._last_error) from exc
        self._last_error = ""
        self._pump = asyncio.create_task(self._pump_stdout())

    async def _kill_ffmpeg(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                chunk = await proc.stdout.read(16 * 1024)
                if not chunk:
                    break
                for queue in list(self._subscribers):
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass
        except asyncio.CancelledError:
            return
        finally:
            err = ""
            if proc.stderr is not None:
                try:
                    err = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
                except Exception:
                    err = ""
            code = proc.returncode
            if err:
                # Не кладём URL с паролем в лог — ffmpeg иногда печатает его сам.
                sanitized = err.replace(self.cfg.camera_password, "***") if self.cfg.camera_password else err
                self._last_error = sanitized.splitlines()[-1][:240]
                log.warning("Камера ffmpeg завершился (%s): %s", code, self._last_error)
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass


def _can_watch(request: Request) -> bool:
    if is_teacher(request):
        return True
    sid = student_session_id(request)
    if not sid:
        return False
    lab = request.app.state.lab
    return lab.student(sid) is not None


@router.get("/status")
async def camera_status(request: Request):
    if not _can_watch(request):
        raise HTTPException(status_code=401, detail="Нужна сессия ученика или учителя")
    relay: CameraRelay = request.app.state.camera
    return JSONResponse(relay.status())


@router.get("/mjpeg")
async def camera_mjpeg(request: Request):
    if not _can_watch(request):
        raise HTTPException(status_code=401, detail="Нужна сессия ученика или учителя")
    relay: CameraRelay = request.app.state.camera
    if not relay.configured:
        raise HTTPException(status_code=404, detail="Камера не настроена")

    try:
        queue = await relay.subscribe()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def frames() -> AsyncIterator[bytes]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    break
                if chunk is None:
                    break
                yield chunk
        finally:
            await relay.unsubscribe(queue)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={FFMPEG_BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
