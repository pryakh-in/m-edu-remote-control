"""Куки-сессии учителя и ученика."""

from __future__ import annotations

import secrets
from collections.abc import Mapping

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

COOKIE_STUDENT = "medu_sid"
COOKIE_TEACHER = "medu_teacher"
COOKIE_PANEL = "medu_panel"

TEACHER_MAX_AGE = 12 * 3600

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="medu-teacher")


def verify_teacher_password(candidate: str) -> bool:
    return secrets.compare_digest(candidate.strip(), settings.teacher_password)


def issue_teacher_cookie(response: Response) -> None:
    token = _serializer.dumps({"role": "teacher"})
    response.set_cookie(
        COOKIE_TEACHER,
        token,
        max_age=TEACHER_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_teacher_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_TEACHER, path="/")


def is_teacher_cookies(cookies: Mapping[str, str]) -> bool:
    token = cookies.get(COOKIE_TEACHER)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=TEACHER_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return True


def is_teacher(request: Request) -> bool:
    return is_teacher_cookies(request.cookies)


def student_session_id(request: Request) -> str | None:
    return request.cookies.get(COOKIE_STUDENT)


def set_student_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        COOKIE_STUDENT,
        session_id,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_student_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_STUDENT, path="/")
