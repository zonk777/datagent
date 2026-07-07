from __future__ import annotations

from typing import Any

from fastapi import Request

from ..db import connect


def actor_from_request(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    actor = getattr(request.state, "admin", None)
    return actor if isinstance(actor, dict) else None


def log_action(
    action: str,
    resource_type: str,
    resource_id: str | int | None = None,
    detail: str = "",
    *,
    status: str = "success",
    actor: dict[str, Any] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO audit_logs(user_id, username, action, resource_type, resource_id, detail, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                actor.get("id") if actor else None,
                actor.get("username") if actor else None,
                action,
                resource_type,
                str(resource_id) if resource_id is not None else None,
                detail,
                status,
            ),
        )


def list_audit_logs(
    *,
    username: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        filters.append("user_id = %s")
        params.append(user_id)
    if username:
        filters.append("username LIKE %s")
        params.append(f"%{username}%")
    if action:
        filters.append("action = %s")
        params.append(action)
    if date_from:
        filters.append("DATE(created_at) >= %s")
        params.append(date_from)
    if date_to:
        filters.append("DATE(created_at) <= %s")
        params.append(date_to)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, user_id, username, action, resource_type, resource_id, detail, status, created_at
                FROM audit_logs
                {where}
                ORDER BY id DESC
                LIMIT %s""",
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]
