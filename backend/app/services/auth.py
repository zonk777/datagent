from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, status

from ..db import connect


SESSION_COOKIE = "dataagent_session"
SESSION_DAYS = 7
PBKDF2_ITERATIONS = 200_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${iterations}${salt}${digest}".format(
        iterations=PBKDF2_ITERATIONS,
        salt=base64.b64encode(salt).decode("ascii"),
        digest=base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _dataset_permissions(user_id: int) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT dataset_id FROM user_dataset_permissions WHERE user_id = %s ORDER BY dataset_id",
            (user_id,),
        ).fetchall()
    return [int(row["dataset_id"]) for row in rows]


def _set_dataset_permissions(user_id: int, dataset_ids: list[int] | None) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM user_dataset_permissions WHERE user_id = %s", (user_id,))
        for dataset_id in dataset_ids or []:
            conn.execute(
                "INSERT IGNORE INTO user_dataset_permissions(user_id, dataset_id) VALUES (%s, %s)",
                (user_id, int(dataset_id)),
            )


def _normalize_role(role: str | None) -> str:
    role = (role or "admin").strip()
    return role if role in {"initial_admin", "admin", "data_analyst", "business_user"} else "business_user"


def _public_admin(row: Any) -> dict:
    role = "initial_admin" if bool(row["is_initial_admin"]) else _normalize_role(row["role"])
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "role": role,
        "dataset_permissions": _dataset_permissions(int(row["id"])),
        "is_initial_admin": bool(row["is_initial_admin"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def authenticate(username: str, password: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT id, username, password_hash, role, is_initial_admin, created_by, created_at
               FROM admin_users WHERE username = %s""",
            (username,),
        ).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return _public_admin(row)


def create_session(admin_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires_at = _utc_now() + timedelta(days=SESSION_DAYS)
    with connect() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE expires_at <= %s", (_iso(_utc_now()),))
        conn.execute(
            "INSERT INTO admin_sessions(token, admin_id, expires_at) VALUES (%s, %s, %s)",
            (token, admin_id, _iso(expires_at)),
        )
    return token, _iso(expires_at)


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = %s", (token,))


def admin_from_session(token: str | None) -> dict | None:
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            """SELECT u.id, u.username, u.role, u.is_initial_admin, u.created_by, u.created_at
               FROM admin_sessions s
               JOIN admin_users u ON u.id = s.admin_id
               WHERE s.token = %s AND s.expires_at > %s""",
            (token, _iso(_utc_now())),
        ).fetchone()
    return _public_admin(row) if row else None


def current_admin(request: Request) -> dict:
    admin = getattr(request.state, "admin", None) or admin_from_session(request.cookies.get(SESSION_COOKIE))
    if not admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return admin


def require_initial_admin(request: Request) -> dict:
    admin = current_admin(request)
    if not admin.get("is_initial_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有初始管理员可以管理账号与权限")
    return admin


def list_admins() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, username, role, is_initial_admin, created_by, created_at FROM admin_users ORDER BY id"
        ).fetchall()
    return [_public_admin(row) for row in rows]


def create_admin(
    username: str,
    password: str,
    creator_id: int,
    role: str = "admin",
    dataset_ids: list[int] | None = None,
) -> dict:
    if not username.strip():
        raise HTTPException(status_code=400, detail="账号不能为空")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 位")
    role = _normalize_role(role)
    if role == "initial_admin":
        role = "admin"
    try:
        with connect() as conn:
            cursor = conn.execute(
                """INSERT INTO admin_users(username, password_hash, role, is_initial_admin, created_by)
                   VALUES (%s, %s, %s, 0, %s)""",
                (username.strip(), hash_password(password), role, creator_id),
            )
            row = conn.execute(
                "SELECT id, username, role, is_initial_admin, created_by, created_at FROM admin_users WHERE id = %s",
                (cursor.lastrowid,),
            ).fetchone()
    except Exception as exc:
        if "UNIQUE" in str(exc).upper() or "Duplicate" in str(exc):
            raise HTTPException(status_code=409, detail="该账号已存在") from exc
        raise
    _set_dataset_permissions(int(row["id"]), dataset_ids)
    return _public_admin(row)


def update_admin(user_id: int, role: str, dataset_ids: list[int] | None = None) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT is_initial_admin FROM admin_users WHERE id = %s", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        if row["is_initial_admin"]:
            role = "initial_admin"
        else:
            role = _normalize_role(role)
        conn.execute("UPDATE admin_users SET role = %s WHERE id = %s", (role, user_id))
    if not row["is_initial_admin"]:
        _set_dataset_permissions(user_id, dataset_ids)
    with connect() as conn:
        updated = conn.execute(
            "SELECT id, username, role, is_initial_admin, created_by, created_at FROM admin_users WHERE id = %s",
            (user_id,),
        ).fetchone()
    return _public_admin(updated)


def change_own_password(actor: dict, current_password: str, new_password: str) -> None:
    """Allow a logged-in user to change their own password."""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少需要 6 位")
    with connect() as conn:
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE id = %s",
            (actor["id"],),
        ).fetchone()
    if not row or not verify_password(current_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    with connect() as conn:
        conn.execute(
            "UPDATE admin_users SET password_hash = %s WHERE id = %s",
            (hash_password(new_password), actor["id"]),
        )


def reset_user_password(user_id: int, new_password: str) -> dict:
    """Reset a user's password (initial_admin only)."""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少需要 6 位")
    with connect() as conn:
        row = conn.execute("SELECT id, username FROM admin_users WHERE id = %s", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        conn.execute(
            "UPDATE admin_users SET password_hash = %s WHERE id = %s",
            (hash_password(new_password), user_id),
        )
    return {"id": int(row["id"]), "username": row["username"]}


def delete_admin(user_id: int, actor_id: int) -> None:
    if user_id == actor_id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    with connect() as conn:
        row = conn.execute("SELECT is_initial_admin FROM admin_users WHERE id = %s", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        if row["is_initial_admin"]:
            raise HTTPException(status_code=400, detail="不能删除初始管理员")
        conn.execute("DELETE FROM admin_users WHERE id = %s", (user_id,))
