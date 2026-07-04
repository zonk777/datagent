from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..db import connect
from .knowledge_documents import chunk_text


CHUNK_MAX_CHARS = 1800
DEFAULT_CONTEXT_CHUNKS = 6
DEFAULT_CONTEXT_CHARS = 26000


def _safe_session_dir(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", session_id)[:96] or "unknown_session"


def _suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix and len(suffix) <= 16 else ""


def _file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _ensure_session(conn: Any, session_id: str, title: str, dataset_id: int | None = None) -> None:
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row:
        if dataset_id is not None:
            conn.execute("UPDATE sessions SET dataset_id = ? WHERE id = ?", (dataset_id, session_id))
        return
    conn.execute(
        "INSERT INTO sessions(id, title, dataset_id) VALUES (?, ?, ?)",
        (session_id, title[:80] or "文件分析", dataset_id),
    )


def save_session_document(
    *,
    session_id: str,
    filename: str,
    content: bytes,
    extracted_text: str,
    dataset_id: int | None = None,
) -> dict[str, Any]:
    """Persist an uploaded file and its extracted text under a conversation.

    The raw file is stored locally for traceability, while the extracted text
    and chunks are stored in the database so future turns can retrieve them.
    """
    cleaned_text = (extracted_text or "").strip()
    if not cleaned_text:
        raise ValueError("文件中没有可持久化的文本内容")

    settings = get_settings()
    digest = _file_sha256(content)
    file_type = _suffix(filename)
    session_dir = settings.session_files_directory / _safe_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    storage_path = session_dir / f"{digest}{file_type}"
    storage_path.write_bytes(content)

    chunks = chunk_text(cleaned_text, max_chars=CHUNK_MAX_CHARS)
    if not chunks:
        chunks = [cleaned_text[:CHUNK_MAX_CHARS]]
    initial_summary = cleaned_text[:1200]

    with connect() as conn:
        _ensure_session(conn, session_id, Path(filename or "文件分析").stem, dataset_id)
        existing = conn.execute(
            "SELECT id FROM session_documents WHERE session_id = ? AND sha256 = ?",
            (session_id, digest),
        ).fetchone()
        if existing:
            document_id = int(existing["id"])
            conn.execute(
                """UPDATE session_documents
                   SET filename = ?, file_type = ?, file_size = ?, storage_path = ?,
                       extracted_text = ?, summary = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (filename, file_type, len(content), str(storage_path), cleaned_text, initial_summary, document_id),
            )
            conn.execute("DELETE FROM session_document_chunks WHERE document_id = ?", (document_id,))
        else:
            cursor = conn.execute(
                """INSERT INTO session_documents(
                       session_id, filename, file_type, file_size, sha256,
                       storage_path, extracted_text, summary
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, filename, file_type, len(content), digest, str(storage_path), cleaned_text, initial_summary),
            )
            document_id = int(cursor.lastrowid)

        conn.executemany(
            """INSERT INTO session_document_chunks(document_id, session_id, chunk_index, content)
               VALUES (?, ?, ?, ?)""",
            [(document_id, session_id, index, chunk) for index, chunk in enumerate(chunks, 1)],
        )
        row = conn.execute("SELECT * FROM session_documents WHERE id = ?", (document_id,)).fetchone()
    return dict(row)


def update_session_document_summary(document_id: int | None, summary: str | None) -> None:
    if not document_id or not summary:
        return
    with connect() as conn:
        conn.execute(
            "UPDATE session_documents SET summary = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (summary[:4000], document_id),
        )


def list_session_documents(session_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, session_id, filename, file_type, file_size, sha256,
                      storage_path, summary, created_at, updated_at
               FROM session_documents
               WHERE session_id = ?
               ORDER BY id""",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_session_document_files(session_id: str) -> None:
    """Best-effort cleanup for raw files when a conversation is deleted."""
    session_dir = get_settings().session_files_directory / _safe_session_dir(session_id)
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def _terms(question: str) -> set[str]:
    normalized = (question or "").lower()
    raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_.%-]+", normalized)
    terms: set[str] = set()
    for term in raw_terms:
        if len(term) <= 1:
            continue
        terms.add(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", term) and len(term) > 6:
            for size in (2, 3, 4):
                for start in range(0, len(term) - size + 1):
                    terms.add(term[start : start + size])
    return terms


def _score_text(question_terms: set[str], title: str, content: str) -> float:
    if not question_terms:
        return 0.0
    title_l = (title or "").lower()
    content_l = (content or "").lower()
    score = 0.0
    for term in question_terms:
        if term in title_l:
            score += 5.0
        if term in content_l:
            score += min(6.0, content_l.count(term)) * max(1.0, min(len(term), 6) / 3)
    return score


def load_session_document_context(
    session_id: str | None,
    question: str,
    *,
    limit: int = DEFAULT_CONTEXT_CHUNKS,
    max_chars: int = DEFAULT_CONTEXT_CHARS,
) -> dict[str, Any]:
    if not session_id:
        return {"documents": [], "chunks": [], "knowledge_refs": [], "combined_text": "", "virtual_filename": ""}

    docs = list_session_documents(session_id)
    if not docs:
        return {"documents": [], "chunks": [], "knowledge_refs": [], "combined_text": "", "virtual_filename": ""}

    q_terms = _terms(question)
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.document_id, c.session_id, c.chunk_index, c.content,
                      d.filename, d.summary
               FROM session_document_chunks c
               JOIN session_documents d ON d.id = c.document_id
               WHERE c.session_id = ?
               ORDER BY c.document_id, c.chunk_index""",
            (session_id,),
        ).fetchall()

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        item = dict(row)
        score = _score_text(q_terms, item.get("filename", ""), item.get("content", ""))
        scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], -int(pair[1].get("chunk_index") or 0)), reverse=True)
    selected = [item for score, item in scored if score > 0][:limit]
    if not selected:
        selected = [item for _, item in scored[:limit]]

    doc_summary_lines = []
    for doc in docs:
        summary = (doc.get("summary") or "").strip()
        doc_summary_lines.append(
            f"文件：{doc.get('filename', '')}\n摘要：{summary[:1200] if summary else '暂无摘要'}"
        )

    parts = ["【当前会话已上传文件】", *doc_summary_lines, "【与本次问题最相关的文件片段】"]
    knowledge_refs: list[dict[str, Any]] = []
    used_chars = sum(len(part) for part in parts)
    for item in selected:
        content = str(item.get("content") or "")
        if used_chars + len(content) > max_chars:
            content = content[: max(0, max_chars - used_chars)]
        if not content:
            break
        label = f"{item.get('filename', '')} / 片段 {item.get('chunk_index')}"
        parts.append(f"\n--- {label} ---\n{content}")
        used_chars += len(content)
        knowledge_refs.append(
            {
                "id": item.get("id"),
                "title": label,
                "content": content[:1800],
                "category": "session_document",
                "score": 1.0,
                "retrieval_mode": "session-document",
                "document_id": item.get("document_id"),
            }
        )
        if used_chars >= max_chars:
            break

    filenames = [doc.get("filename", "") for doc in docs if doc.get("filename")]
    virtual_filename = "、".join(filenames[:3]) + (" 等会话文件.md" if len(filenames) > 3 else ".md")
    return {
        "documents": docs,
        "chunks": selected,
        "knowledge_refs": knowledge_refs,
        "combined_text": "\n\n".join(parts)[:max_chars],
        "virtual_filename": virtual_filename or "会话文件上下文.md",
    }


def should_use_session_documents(question: str, history: list[dict[str, Any]], context: dict[str, Any]) -> bool:
    if not context.get("documents"):
        return False
    q = (question or "").lower()
    explicit_database_terms = (
        "数据库", "数据源", "当前数据集", "数据集", "sql", "表里", "数据表", "业务数据库",
    )
    if any(term in q for term in explicit_database_terms):
        return False

    doc_terms = (
        "文件", "文档", "pdf", "word", "报告", "年报", "刚才", "上次上传", "上传的",
        "这份", "这个", "该报告", "里面", "继续", "总结", "风险", "建议", "依据",
        "根据刚才", "根据文档", "根据报告",
    )
    if any(term in q for term in doc_terms):
        return True

    for item in reversed(history[-6:]):
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        mode = str(payload.get("execution_mode") or "")
        if mode.startswith(("document", "local-document")) or payload.get("document_analyzed"):
            return True
    return False
