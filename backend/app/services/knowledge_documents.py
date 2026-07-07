from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
from docx import Document

from ..db import connect


TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".log", ".ini", ".conf"}
TABLE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls"}
STRUCTURED_SUFFIXES = {".json", ".jsonl", ".xml", ".html", ".htm"}
PRESENTATION_SUFFIXES = {".pptx"}
SUPPORTED_DOCUMENT_SUFFIXES = (
    TEXT_SUFFIXES
    | TABLE_SUFFIXES
    | STRUCTURED_SUFFIXES
    | PRESENTATION_SUFFIXES
    | {".pdf", ".docx", ".doc"}
)


def supported_document_formats() -> list[str]:
    return sorted(SUPPORTED_DOCUMENT_SUFFIXES)


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _dataframe_summary(df: pd.DataFrame, title: str) -> str:
    rows, cols = df.shape
    col_names = [str(item) for item in df.columns]
    null_rates = []
    for column in df.columns[:20]:
        rate = float(df[column].isna().mean()) if rows else 0.0
        null_rates.append(f"{column}: {rate:.1%}")
    preview = df.head(20).fillna("").to_dict(orient="records")
    return "\n".join(
        [
            title,
            f"行数：{rows}，列数：{cols}",
            f"字段：{', '.join(col_names[:50])}",
            f"空值率：{'; '.join(null_rates)}" if null_rates else "空值率：无",
            "前 20 行样例：",
            json.dumps(preview, ensure_ascii=False, default=str, indent=2),
        ]
    )


def _table_to_text(filename: str | None, content: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".csv", ".tsv"}:
        sep = "\t" if suffix == ".tsv" else None
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                df = pd.read_csv(io.BytesIO(content), encoding=encoding, sep=sep)
                break
            except Exception as exc:
                last_error = exc
        else:
            raise ValueError(f"表格文件解析失败：{last_error}") from last_error
        return _dataframe_summary(df, Path(filename or "表格文件").name)

    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None)
    except ImportError as exc:
        raise ValueError("Excel 解析依赖缺失，请安装 openpyxl/xlrd 后重试") from exc
    except Exception as exc:
        raise ValueError(f"Excel 文件解析失败：{exc}") from exc

    parts: list[str] = [f"文件：{Path(filename or 'Excel 文件').name}", f"工作表数量：{len(sheets)}"]
    for sheet_name, df in list(sheets.items())[:8]:
        parts.append(_dataframe_summary(df, f"工作表：{sheet_name}"))
    if len(sheets) > 8:
        parts.append(f"其余 {len(sheets) - 8} 个工作表未展开。")
    return "\n\n".join(parts)


def _json_to_text(filename: str | None, content: bytes) -> str:
    raw = _decode_text(content)
    suffix = Path(filename or "").suffix.lower()
    try:
        if suffix == ".jsonl":
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            return "\n".join(
                [
                    f"文件：{Path(filename or 'JSONL 文件').name}",
                    f"记录数：{len(rows)}",
                    "前 30 条样例：",
                    json.dumps(rows[:30], ensure_ascii=False, default=str, indent=2),
                ]
            )
        parsed = json.loads(raw)
        return "\n".join(
            [
                f"文件：{Path(filename or 'JSON 文件').name}",
                "JSON 内容摘要：",
                json.dumps(parsed, ensure_ascii=False, default=str, indent=2)[:20000],
            ]
        )
    except Exception as exc:
        raise ValueError(f"JSON 文件解析失败：{exc}") from exc


def _html_to_text(content: bytes) -> str:
    raw = _decode_text(content)
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</p>|</div>|</tr>|</h[1-6]>", "\n", raw)
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def _xml_to_text(content: bytes) -> str:
    raw = _decode_text(content)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return raw
    texts = [text.strip() for text in root.itertext() if text and text.strip()]
    return "\n".join(texts)


def _pptx_to_text(content: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("PPTX 文件结构损坏，无法解析") from exc
    slide_names = sorted(
        name for name in archive.namelist()
        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
    )
    parts: list[str] = []
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    for index, name in enumerate(slide_names, 1):
        try:
            root = ET.fromstring(archive.read(name))
        except ET.ParseError:
            continue
        texts = [node.text.strip() for node in root.findall(".//a:t", ns) if node.text and node.text.strip()]
        if texts:
            parts.append(f"第 {index} 页：\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _legacy_doc_to_text(filename: str | None, content: bytes) -> str:
    """Best-effort parser for legacy .doc files on Windows."""
    try:
        import tempfile
        import win32com.client  # type: ignore
    except Exception as exc:
        raise ValueError("旧版 Word .doc 需要本机安装 Microsoft Word 和 pywin32；建议另存为 .docx 后上传") from exc

    suffix = Path(filename or "document.doc").suffix or ".doc"
    tmp_path = None
    word = None
    doc = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        doc = word.Documents.Open(tmp_path)
        return str(doc.Content.Text or "").strip()
    except Exception as exc:
        raise ValueError(f"旧版 Word .doc 解析失败：{exc}") from exc
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        finally:
            if word is not None:
                word.Quit()
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def extract_document_text(filename: str | None, content: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return _decode_text(content)
    if suffix in TABLE_SUFFIXES:
        return _table_to_text(filename, content)
    if suffix in {".json", ".jsonl"}:
        return _json_to_text(filename, content)
    if suffix in {".html", ".htm"}:
        return _html_to_text(content)
    if suffix == ".xml":
        return _xml_to_text(content)
    if suffix == ".pptx":
        return _pptx_to_text(content)
    if suffix == ".docx":
        doc = Document(io.BytesIO(content))
        paragraphs = [paragraph.text.strip() for paragraph in doc.paragraphs if paragraph.text.strip()]
        return "\n".join(paragraphs)
    if suffix == ".doc":
        return _legacy_doc_to_text(filename, content)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("未安装 pypdf，无法解析 PDF；请先执行 pip install pypdf") from exc
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "").strip() for page in reader.pages)
    formats = "、".join(supported_document_formats())
    raise ValueError(f"暂不支持该文件格式：{suffix or '未知'}。当前支持：{formats}")


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    paragraphs = [item.strip() for item in text.replace("\r\n", "\n").split("\n") if item.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(paragraph), max_chars):
                part = paragraph[start : start + max_chars].strip()
                if part:
                    chunks.append(part)
            continue
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def import_knowledge_document(
    *,
    filename: str | None,
    content: bytes,
    title: str | None,
    category: str,
    dataset_id: int | None,
    created_by: int | None = None,
) -> list[dict]:
    text = extract_document_text(filename, content)
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("文档中没有可解析的文字内容")
    base_title = title or Path(filename or "知识文档").stem
    inserted: list[dict] = []
    with connect() as conn:
        for index, chunk in enumerate(chunks, 1):
            chunk_title = base_title if len(chunks) == 1 else f"{base_title} - 片段 {index}"
            cursor = conn.execute(
                "INSERT INTO knowledge_chunks(title, content, category, dataset_id, created_by) VALUES (%s, %s, %s, %s, %s)",
                (chunk_title, chunk, category, dataset_id, created_by),
            )
            row = conn.execute("SELECT * FROM knowledge_chunks WHERE id = %s", (cursor.lastrowid,)).fetchone()
            inserted.append(dict(row))
    return inserted
