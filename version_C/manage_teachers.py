"""교사 계정 관리 CLI.

사용법:
    python -m version_C.manage_teachers create <username> --role teacher|admin
    python -m version_C.manage_teachers list
    python -m version_C.manage_teachers set-password <username>
    python -m version_C.manage_teachers delete <username>
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .auth import create_teacher
from .db import Database, Teacher
from werkzeug.security import generate_password_hash


def _read_password() -> str:
    env_password = os.environ.get("SETUK_NEW_PASSWORD")
    if env_password is not None:
        password = env_password
    elif not sys.stdin.isatty():
        print(
            "터미널이 아닌 환경에서는 SETUK_NEW_PASSWORD 환경변수로 비밀번호를 넘기세요.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    else:
        password = getpass.getpass("비밀번호: ")
        confirm = getpass.getpass("비밀번호 확인: ")
        if password != confirm:
            print("비밀번호가 일치하지 않습니다.", file=sys.stderr)
            raise SystemExit(1)
    if len(password) < 8:
        print("비밀번호는 8자 이상이어야 합니다.", file=sys.stderr)
        raise SystemExit(1)
    return password


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("username")
    create_parser.add_argument("--role", choices=["teacher", "admin"], default="teacher")

    subparsers.add_parser("list")

    password_parser = subparsers.add_parser("set-password")
    password_parser.add_argument("username")

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("username")

    args = parser.parse_args()
    db = Database()
    db.create_all()

    if args.command == "create":
        password = _read_password()
        create_teacher(db, args.username, password, role=args.role)
        print(f"생성했습니다: {args.username} ({args.role})")

    elif args.command == "list":
        with db.session() as session:
            teachers = session.query(Teacher).order_by(Teacher.username).all()
            for teacher in teachers:
                print(f"{teacher.username}\t{teacher.role}\t{teacher.created_at}")

    elif args.command == "set-password":
        password = _read_password()
        with db.session() as session:
            teacher = session.get(Teacher, args.username)
            if teacher is None:
                print(f"계정을 찾을 수 없습니다: {args.username}", file=sys.stderr)
                raise SystemExit(1)
            teacher.password_hash = generate_password_hash(password)
        print("비밀번호를 변경했습니다.")

    elif args.command == "delete":
        with db.session() as session:
            teacher = session.get(Teacher, args.username)
            if teacher is None:
                print(f"계정을 찾을 수 없습니다: {args.username}", file=sys.stderr)
                raise SystemExit(1)
            session.delete(teacher)
        print(f"삭제했습니다: {args.username}")


if __name__ == "__main__":
    main()
