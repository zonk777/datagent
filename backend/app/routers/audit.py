from __future__ import annotations

from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from openpyxl import Workbook

from ..services.audit import list_audit_logs, log_action
from ..services.auth import current_admin


router = APIRouter(prefix="/audit", tags=["audit"])


def _require_audit_access(request: Request) -> dict:
    """All authenticated users can access audit logs, but non-super-admin only see their own."""
    return current_admin(request)


def _is_super_admin(actor: dict) -> bool:
    return bool(actor.get("is_initial_admin"))


@router.get("/logs")
def audit_logs(
    request: Request,
    username: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    actor = _require_audit_access(request)
    # Non-super-admin users can only see their own audit logs
    restrict_user_id: int | None = None
    if not _is_super_admin(actor):
        restrict_user_id = int(actor["id"])
    return list_audit_logs(
        username=username, action=action, date_from=date_from, date_to=date_to,
        limit=limit, user_id=restrict_user_id,
    )


@router.get("/logs/export.xlsx")
def export_audit_logs(
    request: Request,
    username: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Response:
    actor = _require_audit_access(request)
    restrict_user_id: int | None = None
    if not _is_super_admin(actor):
        restrict_user_id = int(actor["id"])
    rows = list_audit_logs(
        username=username, action=action, date_from=date_from, date_to=date_to,
        limit=10000, user_id=restrict_user_id,
    )
    wb = Workbook()
    ws = wb.active
    ws.title = "审计日志"
    headers = ["ID", "用户", "操作类型", "资源类型", "资源ID", "详情", "状态", "时间"]
    ws.append(headers)
    for row in rows:
        ws.append(
            [
                row.get("id"),
                row.get("username") or "",
                row.get("action"),
                row.get("resource_type"),
                row.get("resource_id") or "",
                row.get("detail") or "",
                row.get("status"),
                row.get("created_at"),
            ]
        )
    for column in ws.columns:
        letter = column[0].column_letter
        ws.column_dimensions[letter].width = min(45, max(12, max(len(str(cell.value or "")) for cell in column) + 2))
    out = BytesIO()
    wb.save(out)
    log_action("export_audit", "audit", None, f"导出 {len(rows)} 条审计日志", actor=actor)
    filename = quote("数据智能体服务系统-审计日志.xlsx")
    return Response(
        out.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
