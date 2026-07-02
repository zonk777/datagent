import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from ..db import connect
from ..models import AnalysisResponse, ChatRequest, SessionCreate
from ..services.analyzer import analyze, analyze_react, analyze_stream, analyze_to_report_stream, analyze_with_document
from ..services.analysis_planner import plan_analysis
from ..services.audit import log_action
from ..services.auth import current_admin
from ..services.data_profiler import profile_dataset
from ..services.error_messages import format_analysis_error
from ..services.meta_router import route_intent
from ..services.permissions import ensure_dataset_access, first_accessible_dataset_id
from ..services.reports import (
    apply_chart_options,
    build_docx_report,
    build_html_report,
    build_markdown_report,
    build_multi_section_pdf,
    build_pdf_report,
    load_report_data,
)


router = APIRouter(tags=["agent"])


def _message_dict(row) -> dict:
    item = dict(row)
    if item.get("payload"):
        try:
            item["payload"] = json.loads(item["payload"])
        except json.JSONDecodeError:
            item["payload"] = None
    return item


@router.get("/sessions")
def list_sessions(request: Request, limit: int = Query(default=30, ge=1, le=100)) -> list[dict]:
    current_admin(request)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.id, s.title, s.dataset_id, s.created_at, s.updated_at,
                      COUNT(m.id) AS message_count,
                      (SELECT content FROM messages lm WHERE lm.session_id = s.id ORDER BY lm.id DESC LIMIT 1) AS last_message
               FROM sessions s
               LEFT JOIN messages m ON m.session_id = s.id
               GROUP BY s.id
               ORDER BY s.updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.post("/sessions", status_code=201)
def create_session(payload: SessionCreate, request: Request) -> dict:
    actor = current_admin(request)
    if payload.dataset_id:
        ensure_dataset_access(actor, payload.dataset_id)
    session_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, title, dataset_id, user_id) VALUES (?, ?, ?, ?)",
            (session_id, payload.title, payload.dataset_id, actor["id"]),
        )
    return {"id": session_id, "title": payload.title, "dataset_id": payload.dataset_id}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str, request: Request) -> dict:
    current_admin(request)
    with connect() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        rows = conn.execute(
            "SELECT id, role, content, payload, created_at FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    result = dict(session)
    result["messages"] = [_message_dict(row) for row in rows]
    return result


@router.get("/sessions/{session_id}/messages")
def session_messages(session_id: str, request: Request) -> list[dict]:
    return session_detail(session_id, request)["messages"]


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, request: Request) -> None:
    actor = current_admin(request)
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    log_action("delete_session", "session", session_id, "删除历史对话", actor=actor)


@router.post("/agent/chat/report/stream")
async def chat_report_stream(
    request: Request,
    question: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    dataset_id: int | None = Form(default=None),
    session_id: str | None = Form(default=None),
):
    """Phase 2: Full report pipeline with SSE streaming — plan → execute → narrative."""
    actor = current_admin(request)
    ds_id = dataset_id or first_accessible_dataset_id(actor)
    if ds_id:
        ensure_dataset_access(actor, ds_id)

    file_bytes = await file.read() if file and file.filename else None
    filename = file.filename if file and file.filename else ""

    async def event_generator():
        try:
            async for event in analyze_to_report_stream(
                question=question or "全面分析",
                session_id=session_id,
                dataset_id=ds_id,
                filename=filename,
                file_content=file_bytes,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            log_action("analysis_request", "session", session_id or "", f"[报告流] {question}", actor=actor)
        except Exception as exc:
            detail = format_analysis_error(exc, operation="报告分析", filename=filename, dataset_id=ds_id)
            yield f"data: {json.dumps({'type': 'error', 'message': detail}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/agent/report/plan")
async def report_plan(
    request: Request,
    question: str = Form(default=""),
    dataset_id: int | None = Form(default=None),
) -> dict:
    """Phase 1: Generate a multi-dimensional analysis plan for user preview.

    Returns {plan, persona, clarification} for frontend to display.
    """
    actor = current_admin(request)
    ds_id = dataset_id or first_accessible_dataset_id(actor)
    if ds_id:
        ensure_dataset_access(actor, ds_id)

    data_profile = profile_dataset(ds_id) if ds_id else "数据集未指定"
    intent = await route_intent(question or "全面分析", data_profile)

    if not intent["info_sufficient"] and intent.get("clarification"):
        return {"phase": "clarify", "clarification": intent["clarification"], "persona": intent["matched_persona"]}

    plan = await plan_analysis(question, data_profile, intent["matched_persona"])
    return {"phase": "plan", "plan": plan, "persona": intent["matched_persona"], "intent": intent}


@router.post("/agent/chat/with-file", response_model=AnalysisResponse)
async def chat_with_file(
    request: Request,
    file: UploadFile = File(...),
    question: str = Form(default=""),
    session_id: str | None = Form(default=None),
    dataset_id: int | None = Form(default=None),
) -> dict:
    """Upload a document (PDF/Word/MD/TXT) and analyze it in one step."""
    actor = current_admin(request)
    dataset_id = dataset_id or first_accessible_dataset_id(actor)
    if dataset_id:
        ensure_dataset_access(actor, dataset_id)
    if not file.filename:
        raise HTTPException(status_code=400, detail="请选择要分析的文件")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    try:
        result = await analyze_with_document(
            question=question or f"分析这份文档: {file.filename}",
            session_id=session_id,
            dataset_id=dataset_id,
            filename=file.filename,
            file_content=content,
        )
        log_action("analysis_request", "session", result.get("session_id"), f"[文件分析] {file.filename}", actor=actor)
        return result
    except Exception as exc:
        detail = format_analysis_error(exc, operation="文件分析", filename=file.filename, dataset_id=dataset_id)
        log_action("analysis_request", "session", session_id or "", detail, status="failed", actor=actor)
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/agent/chat", response_model=AnalysisResponse)
async def chat(payload: ChatRequest, request: Request) -> dict:
    actor = current_admin(request)
    dataset_id = payload.dataset_id or first_accessible_dataset_id(actor)
    if dataset_id:
        ensure_dataset_access(actor, dataset_id)
    try:
        result = await analyze(payload.question, payload.session_id, dataset_id)
        log_action("analysis_request", "session", result.get("session_id"), payload.question, actor=actor)
        return result
    except Exception as exc:
        detail = format_analysis_error(exc, operation="智能分析", dataset_id=dataset_id)
        log_action("analysis_request", "session", payload.session_id, detail, status="failed", actor=actor)
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/agent/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request):
    """SSE streaming analysis with plan steps and incremental thinking."""
    actor = current_admin(request)
    dataset_id = payload.dataset_id or first_accessible_dataset_id(actor)
    if dataset_id:
        ensure_dataset_access(actor, dataset_id)

    async def event_generator():
        try:
            async for event in analyze_stream(payload.question, payload.session_id, dataset_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            log_action("analysis_request", "session", payload.session_id or "", payload.question, actor=actor)
        except Exception as exc:
            detail = format_analysis_error(exc, operation="智能分析", dataset_id=dataset_id)
            log_action("analysis_request", "session", payload.session_id or "", detail, status="failed", actor=actor)
            yield f"data: {json.dumps({'type': 'error', 'message': detail}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/agent/chat/react", response_model=AnalysisResponse)
async def chat_react(payload: ChatRequest, request: Request) -> dict:
    """ReAct Agent — LLM-driven tool calling loop with hardcoded tool schemas."""
    actor = current_admin(request)
    dataset_id = payload.dataset_id or first_accessible_dataset_id(actor)
    if dataset_id:
        ensure_dataset_access(actor, dataset_id)
    try:
        result = await analyze_react(payload.question, payload.session_id, dataset_id)
        log_action("analysis_request", "session", result.get("session_id"), f"[ReAct] {payload.question}", actor=actor)
        return result
    except Exception as exc:
        detail = format_analysis_error(exc, operation="ReAct 智能分析", dataset_id=dataset_id)
        log_action("analysis_request", "session", payload.session_id, detail, status="failed", actor=actor)
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/agent/chat/react/mcp", response_model=AnalysisResponse)
async def chat_react_mcp(payload: ChatRequest, request: Request) -> dict:
    """ReAct + MCP — dynamic tool discovery via MCP protocol."""
    actor = current_admin(request)
    dataset_id = payload.dataset_id or first_accessible_dataset_id(actor)
    if dataset_id:
        ensure_dataset_access(actor, dataset_id)
    try:
        result = await analyze_react(payload.question, payload.session_id, dataset_id, use_mcp=True)
        log_action("analysis_request", "session", result.get("session_id"), f"[ReAct+MCP] {payload.question}", actor=actor)
        return result
    except Exception as exc:
        detail = format_analysis_error(exc, operation="ReAct/MCP 智能分析", dataset_id=dataset_id)
        log_action("analysis_request", "session", payload.session_id, detail, status="failed", actor=actor)
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/agent/chat/react/stream")
async def chat_react_stream(payload: ChatRequest, request: Request):
    """ReAct + MCP + SSE streaming — combines thinking steps with real-time MCP tool events."""
    actor = current_admin(request)
    dataset_id = payload.dataset_id or first_accessible_dataset_id(actor)
    if dataset_id:
        ensure_dataset_access(actor, dataset_id)

    async def event_generator():
        import time as _time
        yield f"data: {json.dumps({'type': 'plan', 'steps': ['意图分类', '工具调用', '数据查询', '洞察生成'], 'intent': '分析中', 'answer_type': 'data_analysis'}, ensure_ascii=False)}\n\n"

        try:
            result = await analyze_react(payload.question, payload.session_id, dataset_id, use_mcp=True)

            # Emit plan steps as they happened
            for step in result.get("plan", []):
                step_title = "Step " + str(step.get('step','')) + ": " + str(step.get('tool',''))
                yield "data: " + json.dumps({"type": "step", "step_id": step.get("step"), "title": step_title, "status": "completed", "detail": step.get("args_summary","")}, ensure_ascii=False) + "\n\n"

            # Emit thinking/reasoning if available
            insights = result.get("insights", [])
            for insight in insights[:2]:
                yield f"data: {json.dumps({'type': 'thinking', 'content': insight}, ensure_ascii=False)}\n\n"

            # Emit final result
            yield f"data: {json.dumps({'type': 'result', 'data': result}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

            log_action("analysis_request", "session", result.get("session_id"), f"[ReAct+Stream] {payload.question}", actor=actor)
        except Exception as exc:
            detail = format_analysis_error(exc, operation="ReAct/MCP 流式分析", dataset_id=dataset_id)
            yield f"data: {json.dumps({'type': 'error', 'message': detail}, ensure_ascii=False)}\n\n"
            log_action("analysis_request", "session", payload.session_id, detail, status="failed", actor=actor)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/agent/report/pdf")
async def download_multi_section_pdf(request: Request) -> Response:
    """Phase 3: Generate multi-section PDF from report sections data."""
    body = await request.json()
    actor = current_admin(request)

    pdf_bytes = build_multi_section_pdf(
        report_title=body.get("title", "数据分析报告"),
        sections=body.get("sections", []),
        executive_summary=body.get("executive_summary", ""),
        data_source=body.get("data_source", ""),
        sql_list=body.get("sql_list"),
    )
    log_action("export_report", "report", None, "Multi-section PDF", actor=actor)
    return Response(pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename*=UTF-8''%E6%95%B0%E6%8D%AE%E6%99%BA%E8%83%BD%E4%BD%93%E5%88%86%E6%9E%90%E6%8A%A5%E5%91%8A.pdf"})


def _report_or_404(session_id: str):
    try:
        return load_report_data(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _report_with_chart_options(session_id: str, request: Request):
    data = _report_or_404(session_id)
    return apply_chart_options(data, request.query_params.get("chart_options"))


def _attachment(content: bytes, media_type: str, filename: str) -> Response:
    encoded = quote(filename)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@router.get("/reports/{session_id}.html", response_class=HTMLResponse)
def report_html(session_id: str, request: Request) -> str:
    actor = current_admin(request)
    data = _report_with_chart_options(session_id, request)
    log_action("export_report", "session", session_id, "HTML", actor=actor)
    return build_html_report(data)


@router.get("/reports/{session_id}.docx")
def report_docx(session_id: str, request: Request) -> Response:
    actor = current_admin(request)
    data = _report_with_chart_options(session_id, request)
    log_action("export_report", "session", session_id, "Word", actor=actor)
    return _attachment(
        build_docx_report(data),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"数据智能体分析报告_{session_id}.docx",
    )


@router.get("/reports/{session_id}.pdf")
def report_pdf(session_id: str, request: Request) -> Response:
    actor = current_admin(request)
    data = _report_with_chart_options(session_id, request)
    log_action("export_report", "session", session_id, "PDF", actor=actor)
    return _attachment(
        build_pdf_report(data),
        "application/pdf",
        f"数据智能体分析报告_{session_id}.pdf",
    )


@router.get("/reports/{session_id}.md")
def report_markdown(session_id: str, request: Request) -> Response:
    actor = current_admin(request)
    data = _report_with_chart_options(session_id, request)
    log_action("export_report", "session", session_id, "Markdown", actor=actor)
    return _attachment(
        build_markdown_report(data),
        "text/markdown; charset=utf-8",
        f"数据智能体分析报告_{session_id}.md",
    )
