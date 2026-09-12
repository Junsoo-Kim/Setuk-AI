from __future__ import annotations

from functools import wraps

from flask import abort, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import ROLE_ADMIN, Database, Teacher


def create_teacher(db: Database, username: str, password: str, role: str = "teacher") -> None:
    with db.session() as db_session:
        if db_session.get(Teacher, username) is not None:
            raise ValueError(f"이미 존재하는 계정입니다: {username}")
        db_session.add(
            Teacher(username=username, password_hash=generate_password_hash(password), role=role)
        )


def verify_login(db: Database, username: str, password: str) -> str | None:
    with db.session() as db_session:
        teacher = db_session.get(Teacher, username)
        if teacher is None or not check_password_hash(teacher.password_hash, password):
            return None
        return teacher.role


def current_username() -> str | None:
    return session.get("username")


def current_role() -> str | None:
    return session.get("role")


def is_admin() -> bool:
    return current_role() == ROLE_ADMIN


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_username() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_username() is None:
            return redirect(url_for("login", next=request.path))
        if not is_admin():
            abort(403, "관리자만 접근할 수 있습니다.")
        return view(*args, **kwargs)

    return wrapped


def assert_owner_or_admin(owner: str | None) -> None:
    if is_admin():
        return
    if owner is None or owner != current_username():
        abort(403, "다른 사용자의 작업에는 접근할 수 없습니다.")
