from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings
from .knowledge_documents import chunk_text
from .planner import build_plan_steps, plan_titles
from .intent_classifier import IntentResult


MAX_DIRECT_CHARS = 12000
CHUNK_CHARS = 5500
MAX_CHUNKS = 10
ALLOWED_DOCUMENT_CHART_TYPES = {"bar", "line", "pie", "scatter", "area", "radar"}
MAX_DOCUMENT_CHART_ROWS = 40
MAX_DOCUMENT_CHART_SECTIONS = 6


def _coerce_chart_value(value: Any) -> str | float | int | None:
    """Normalize values returned by the LLM so ECharts can draw them reliably."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 4)

    text = str(value).strip()
    if not text:
        return ""

    compact = text.replace(",", "").replace("，", "")
    match = re.search(r"-?\d+(?:\.\d+)?", compact)
    has_numeric_unit = any(unit in compact for unit in ("%", "％", "元", "万", "亿", "条", "个", "户", "倍", "页"))
    if match and (has_numeric_unit or re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", compact)):
        number = float(match.group(0))
        return int(number) if number.is_integer() else round(number, 4)
    return text[:160]


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    return columns


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _guess_x_field(rows: list[dict[str, Any]], columns: list[str]) -> str:
    for column in columns:
        values = [row.get(column) for row in rows]
        if any(value not in (None, "") and not _is_numeric(value) for value in values):
            return column
    return columns[0] if columns else ""


def _guess_y_field(rows: list[dict[str, Any]], columns: list[str], x_field: str) -> str:
    for column in columns:
        if column == x_field:
            continue
        values = [row.get(column) for row in rows]
        if any(_is_numeric(value) for value in values):
            return column
    return next((column for column in columns if column != x_field), "")


def _empty_document_chart(title: str) -> dict[str, Any]:
    return {
        "type": "none",
        "title": f"{title} - 文档分析",
        "x_field": "",
        "y_field": "",
        "series_name": "",
        "series_field": None,
        "series_fields": [],
    }


def _clean_metric_label(prefix: str) -> str:
    prefix = prefix.replace("\n", " ")
    parts = [part.strip() for part in re.split(r"[。；;，,、]", prefix) if part.strip()]
    label = parts[-1] if parts else prefix
    label = label.strip(" ：:（）()[]【】+-=~约达为")
    label = re.sub(r"^(且|其中|同时|但|而|并|以及|另外|此外)", "", label)
    label = re.sub(r"(均)?(超过|高达|达到|达|为|是|升至|增至|约|约为|占比|占)$", "", label)
    label = label.strip(" ：:（）()[]【】+-=~约达为")
    if "（" in label and not label.endswith("）"):
        before, after = label.rsplit("（", 1)
        label = after if len(after) >= 2 else before
    if "(" in label and not label.endswith(")"):
        before, after = label.rsplit("(", 1)
        label = after if len(after) >= 2 else before
    label = re.sub(r"\s+", "", label)
    label = re.sub(r"^[\d.]+$", "", label)
    return label[-18:] if len(label) > 18 else label


def _fallback_chart_from_insights(title: str, insights: list[str]) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Build a small chart from numeric facts in model-generated insights."""
    rows: list[dict[str, Any]] = []
    pattern = re.compile(r"(-?\d+(?:\.\d+)?)\s*(%|％|亿元|万元|元|亿|万|条|个|户|倍)")
    for insight in insights:
        for match in pattern.finditer(insight):
            raw_value, unit = match.groups()
            prefix = insight[max(0, match.start() - 42):match.start()]
            label = _clean_metric_label(prefix)
            try:
                value = float(raw_value)
            except ValueError:
                continue
            if not label or any(row["指标"] == label for row in rows):
                continue
            rows.append({"指标": label, "数值": int(value) if value.is_integer() else round(value, 4), "单位": unit})
            if len(rows) >= 12:
                break
        if len(rows) >= 12:
            break

    if len(rows) < 2:
        return [], [], _empty_document_chart(title)

    return (
        ["指标", "数值", "单位"],
        rows,
        {
            "type": "bar",
            "title": f"{title} - 关键指标提取",
            "x_field": "指标",
            "y_field": "数值",
            "series_name": "数值",
            "series_field": None,
            "series_fields": [],
            "recommendation": {
                "type": "bar",
                "source": "document-insight-extraction",
                "reason": "从 DeepSeek 生成的文档结论中提取可量化指标用于直观对比。",
                "confidence": 0.62,
            },
        },
    )


def _chart_payload_from_chart(
    chart: dict[str, Any],
    title: str,
    insights: list[str],
    *,
    source: str = "document-llm",
    confidence: float = 0.78,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    raw_rows = chart.get("rows") if isinstance(chart.get("rows"), list) else []
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows[:MAX_DOCUMENT_CHART_ROWS]:
        if not isinstance(raw_row, dict):
            continue
        row: dict[str, Any] = {}
        for key, value in raw_row.items():
            column = str(key).strip()
            normalized = _coerce_chart_value(value)
            if column and normalized not in (None, ""):
                row[column] = normalized
        if row:
            rows.append(row)

    if not rows:
        return _fallback_chart_from_insights(title, insights)

    columns = _columns_from_rows(rows)
    x_field = str(chart.get("x_field") or chart.get("x") or "").strip()
    y_field = str(chart.get("y_field") or chart.get("y") or "").strip()
    if x_field not in columns:
        x_field = _guess_x_field(rows, columns)
    if y_field not in columns:
        y_field = _guess_y_field(rows, columns, x_field)
    if not x_field or not y_field:
        return _fallback_chart_from_insights(title, insights)

    chart_type = str(chart.get("type") or "bar").lower()
    if chart_type not in ALLOWED_DOCUMENT_CHART_TYPES:
        chart_type = "bar"
    series_field = chart.get("series_field")
    series_field = str(series_field).strip() if series_field else None
    if series_field not in columns:
        series_field = None
    series_fields = chart.get("series_fields") if isinstance(chart.get("series_fields"), list) else []
    series_fields = [str(field).strip() for field in series_fields if str(field).strip() in columns]
    if series_field and not series_fields:
        series_fields = [series_field]

    chart_title = str(chart.get("title") or f"{title} - 关键指标图表").strip()
    return (
        columns,
        rows,
        {
            "type": chart_type,
            "title": chart_title,
            "x_field": x_field,
            "y_field": y_field,
            "series_name": str(chart.get("series_name") or y_field),
            "series_field": series_field,
            "series_fields": series_fields,
            "recommendation": {
                "type": chart_type,
                "source": source,
                "reason": "DeepSeek 从上传文档中抽取了可量化字段，并推荐用于图表展示。",
                "confidence": confidence,
            },
        },
    )


def _chart_payload_from_llm_result(
    result: dict[str, Any],
    title: str,
    insights: list[str],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    chart = result.get("chart") if isinstance(result.get("chart"), dict) else {}
    return _chart_payload_from_chart(chart, title, insights)


def _section_payload(
    *,
    section_id: str,
    title: str,
    description: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    chart: dict[str, Any],
    insights: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": section_id,
        "title": title,
        "description": description,
        "columns": columns,
        "rows": rows,
        "chart": chart,
        "insights": insights or [],
    }


def _chart_sections_from_llm_result(
    result: dict[str, Any],
    title: str,
    insights: list[str],
) -> list[dict[str, Any]]:
    raw_sections = result.get("chart_sections") if isinstance(result.get("chart_sections"), list) else []
    omitted_from_model = result.get("omitted_chart_sections")
    omitted_titles: list[str] = []
    if isinstance(omitted_from_model, list):
        omitted_titles.extend(str(item).strip() for item in omitted_from_model if str(item).strip())
    if len(raw_sections) > MAX_DOCUMENT_CHART_SECTIONS:
        omitted_titles.extend(
            str(item.get("title") or item.get("id") or f"图表 {index}").strip()
            for index, item in enumerate(raw_sections[MAX_DOCUMENT_CHART_SECTIONS:], MAX_DOCUMENT_CHART_SECTIONS + 1)
            if isinstance(item, dict)
        )
    if omitted_titles:
        note = (
            f"图表数量限制：本次最多展示 {MAX_DOCUMENT_CHART_SECTIONS} 张图，"
            f"已省略：{'、'.join(omitted_titles[:8])}"
            + ("等。" if len(omitted_titles) > 8 else "。")
            + "你可以继续追问其中某个维度单独展开。"
        )
        if note not in insights:
            insights.append(note)
    sections: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_sections[:MAX_DOCUMENT_CHART_SECTIONS], 1):
        if not isinstance(raw, dict):
            continue
        section_title = str(raw.get("title") or f"{title} - 图表 {index}").strip()
        section_insights = [
            str(item).strip()
            for item in (raw.get("insights") if isinstance(raw.get("insights"), list) else [])
            if str(item).strip()
        ]
        chart = raw.get("chart") if isinstance(raw.get("chart"), dict) else raw
        columns, rows, chart_spec = _chart_payload_from_chart(
            chart,
            section_title,
            section_insights or insights,
            source="document-llm-section",
            confidence=0.82,
        )
        if not rows or chart_spec.get("type") == "none":
            continue
        sections.append(
            _section_payload(
                section_id=str(raw.get("id") or f"doc-section-{index}"),
                title=section_title,
                description=str(raw.get("description") or raw.get("summary") or "").strip(),
                columns=columns,
                rows=rows,
                chart=chart_spec,
                insights=section_insights,
            )
        )

    if sections:
        return sections

    columns, rows, chart_spec = _chart_payload_from_llm_result(result, title, insights)
    if rows and chart_spec.get("type") != "none":
        return [
            _section_payload(
                section_id="doc-section-primary",
                title=chart_spec.get("title") or f"{title} - 关键指标",
                description="文档中抽取的核心可视化指标。",
                columns=columns,
                rows=rows,
                chart=chart_spec,
                insights=insights[:2],
            )
        ]
    return []


def wants_database_context(question: str) -> bool:
    """Only combine uploaded files with business DB when the user says so."""
    signals = (
        "结合数据源",
        "结合数据库",
        "结合业务数据",
        "结合当前数据",
        "结合数据表",
        "查询数据库",
        "查询数据表",
        "用当前数据源",
        "用业务数据库",
        "和数据库一起",
        "和数据源一起",
    )
    text = question or ""
    return any(signal in text for signal in signals)


def _safe_json_loads(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise


def _llm_error_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return "模型接口认证失败：API Key 无效、过期或与当前平台不匹配。"
        if status == 402:
            return "模型接口余额不足或套餐不可用：请检查 API 账户余额、额度或计费状态。"
        if status == 429:
            return "模型接口请求过于频繁：请稍后重试或降低并发。"
        if 500 <= status < 600:
            return "模型服务暂时异常：对方接口返回服务器错误。"
        return f"模型接口调用失败：HTTP {status}。"
    if isinstance(exc, httpx.TimeoutException):
        return "模型接口响应超时：文件较长或网络较慢，请稍后重试。"
    if isinstance(exc, httpx.HTTPError):
        return f"模型接口网络错误：{exc}"
    return f"模型返回内容无法解析：{exc}"


async def _llm_json(messages: list[dict[str, str]], timeout: int = 60) -> dict[str, Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={"model": settings.llm_model, "temperature": 0.2, "messages": messages},
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
    parsed = _safe_json_loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的文档分析结果不是 JSON 对象")
    return parsed


async def _summarize_chunk(
    *,
    title: str,
    question: str,
    index: int,
    total: int,
    chunk: str,
) -> str:
    settings = get_settings()
    if not settings.llm_configured:
        return chunk[:1200]

    prompt = {
        "document_title": title,
        "user_question": question,
        "chunk_index": index,
        "chunk_total": total,
        "chunk_text": chunk,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是企业文档分析助手。请阅读文档片段，提取与用户问题相关的事实。"
                "输出中文要点，不要编造；保留重要数字、时间、主体、结论、风险和建议。"
                "如果片段是表格/CSV/Excel 摘要，请关注字段、样例、规模、异常和可分析维度。"
            ),
        },
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={"model": settings.llm_model, "temperature": 0.2, "messages": messages},
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


def _fallback_document_payload(
    *,
    session_id: str,
    title: str,
    question: str,
    text: str,
    reason: str = "",
) -> dict[str, Any]:
    excerpt = text[:1800]
    if reason:
        message = f"已解析《{title}》，但暂时无法完成深度模型分析：{reason}"
    else:
        message = f"已解析《{title}》，但当前未配置可用的大模型，因此仅返回文档前部摘要。"
    insight = f"{message}\n\n{excerpt}"
    plan_steps = build_plan_steps(
        intent=IntentResult("knowledge_qa", 0.8, "rules", "上传文件分析"),
        answer_type="knowledge_qa",
        execution_mode="local-document-parser",
        dataset_name=title,
        chart_type="none",
    )
    return {
        "session_id": session_id,
        "message": message,
        "intent": "文档分析",
        "intent_label": "knowledge_qa",
        "intent_confidence": 0.8,
        "intent_method": "rules",
        "intent_reason": "上传文件触发文档分析",
        "sql_source": None,
        "plan": plan_titles(plan_steps),
        "plan_steps": plan_steps,
        "sql": "",
        "columns": [],
        "rows": [],
        "chart": {
            "type": "none",
            "title": f"{title} - 文档分析",
            "x_field": "",
            "y_field": "",
            "series_name": "",
            "series_field": None,
            "series_fields": [],
        },
        "insights": [insight],
        "knowledge_refs": [{"id": 0, "title": title, "content": excerpt, "category": "uploaded_file", "score": 1.0}],
        "execution_mode": "local-document-parser",
        "answer_type": "knowledge_qa",
        "context_applied": False,
        "effective_question": question,
    }


async def analyze_uploaded_document(
    *,
    session_id: str,
    filename: str,
    text: str,
    question: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Analyze an uploaded file without defaulting to database queries."""
    title = Path(filename or "上传文件").stem
    effective_question = question or "请分析这份文件，提炼关键信息、主要结论、风险点和建议。"
    cleaned_text = text.strip()
    if not cleaned_text:
        raise ValueError("文件中没有可解析的文字内容；如果是扫描版 PDF，请先进行 OCR 后再上传。")

    settings = get_settings()
    if not settings.llm_configured:
        return _fallback_document_payload(session_id=session_id, title=title, question=effective_question, text=cleaned_text)

    chunks = chunk_text(cleaned_text, max_chars=CHUNK_CHARS)
    if len(chunks) > MAX_CHUNKS:
        chunks = chunks[:MAX_CHUNKS]
        truncated_notice = f"\n\n注意：文档较长，本次先分析前 {MAX_CHUNKS} 个片段。"
    else:
        truncated_notice = ""

    if len(cleaned_text) <= MAX_DIRECT_CHARS:
        notes = cleaned_text
        source_mode = "全文直读"
    else:
        source_mode = f"分段提炼（{len(chunks)} 个片段）"
        summaries = []
        try:
            for index, item in enumerate(chunks, 1):
                summaries.append(
                    await _summarize_chunk(
                        title=title,
                        question=effective_question,
                        index=index,
                        total=len(chunks),
                        chunk=item,
                    )
                )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            return _fallback_document_payload(
                session_id=session_id,
                title=title,
                question=effective_question,
                text=cleaned_text,
                reason=_llm_error_message(exc),
            )
        notes = "\n\n".join(f"片段 {idx}：\n{summary}" for idx, summary in enumerate(summaries, 1))

    recent_history = []
    for item in (history or [])[-4:]:
        role = item.get("role")
        if role in ("user", "assistant") and item.get("content"):
            recent_history.append({"role": role, "content": str(item["content"])[:1000]})

    final_messages = [
        {
            "role": "system",
            "content": (
                "你是企业文件分析专家。用户上传了一个文件，你必须基于文件内容回答，"
                "不要默认去分析业务数据库。除非用户明确要求结合数据库，否则不要生成 SQL、不要讨论当前数据源。"
                "输出必须是 JSON 对象，字段：summary 字符串、insights 字符串数组、document_type 字符串、"
                "recommended_actions 字符串数组。"
                "如果用户要求报告，请给出多维度报告；如果用户要求提取信息，请按要求提取。"
                "所有结论必须来自文件内容，不确定处要说明“文件中未明确给出”。"
            ),
        },
        {
            "role": "system",
            "content": (
                "请在 JSON 中额外返回 chart 对象，用于前端直接画图。"
                "chart 结构必须为："
                "{\"type\":\"bar|line|pie|scatter|area|radar|none\",\"title\":\"图表标题\","
                "\"x_field\":\"维度字段\",\"y_field\":\"数值字段\",\"series_field\":null,"
                "\"series_name\":\"系列名称\",\"rows\":[{\"维度字段\":\"指标名\",\"数值字段\":123.45}]}。"
                "如果文档中有营收、利润、增长率、占比、客户数、现金流、资产等可量化指标，优先抽取 3-12 条做图；"
                "百分比只填数字，例如 45.88，不要填 '45.88%'；金额可保留原单位对应的数字，例如亿元填 6.39。"
                "如果没有任何可视化价值的数据，chart.type 填 none 且 rows 为空。"
            ),
        },
        {
            "role": "system",
            "content": (
                "如果用户要求“完整分析、全面分析、多维度分析、分析报告、经营报告、财务报告”等，"
                "请额外返回 chart_sections 数组，表示多组图表。每个 section 结构为："
                "{\"id\":\"唯一ID\",\"title\":\"维度名称\",\"description\":\"该图说明\","
                "\"insights\":[\"该图对应的一句话洞察\"],"
                "\"chart\":{\"type\":\"bar|line|pie|scatter|area|radar|none\",\"title\":\"图表标题\","
                "\"x_field\":\"维度字段\",\"y_field\":\"数值字段\",\"series_field\":null,"
                "\"series_name\":\"系列名称\",\"rows\":[{\"维度字段\":\"指标名\",\"数值字段\":123.45}]}}。"
                "尽量生成 3-6 个 section，例如：增长指标、收入结构、客户集中度、现金流、资产负债、研发投入。"
                "最多只能生成 6 个 section；如果你认为还有其他应该生成但因数量限制未展示的图，"
                "请额外返回 omitted_chart_sections 字符串数组，只写被省略的图表名称。"
                "每个 section 只放同一量纲或同一主题的数据；如果单位不同，可以拆成不同 section。"
                "仍然要保留顶层 chart，用最重要的一组图作为默认展示。"
            ),
        },
        *recent_history,
        {
            "role": "user",
            "content": json.dumps(
                {
                    "file_name": filename,
                    "document_title": title,
                    "analysis_mode": source_mode,
                    "user_question": effective_question,
                    "document_notes": notes[:28000],
                    "truncated_notice": truncated_notice,
                    "visualization_instruction": (
                        "请根据文档里的数字字段生成 chart.rows。每行必须是扁平 JSON 对象，"
                        "至少包含一个维度字段和一个数值字段，数值字段必须是 number 类型。"
                        "如果是完整/多维分析，请把不同主题分别放入 chart_sections，每组都带 chart.rows。"
                    ),
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        result = await _llm_json(final_messages, timeout=80)
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _fallback_document_payload(
            session_id=session_id,
            title=title,
            question=effective_question,
            text=cleaned_text,
            reason=_llm_error_message(exc),
        )
    summary = str(result.get("summary") or "").strip()
    insights = [str(item).strip() for item in result.get("insights", []) if str(item).strip()]
    actions = [str(item).strip() for item in result.get("recommended_actions", []) if str(item).strip()]
    if actions:
        insights.extend([f"建议：{item}" for item in actions[:3]])
    if truncated_notice:
        insights.append(truncated_notice.strip())
    if not summary and insights:
        summary = insights[0]
    if not insights and summary:
        insights = [summary]
    if not summary:
        summary = "文件已解析，但模型没有生成有效结论。请换一种更具体的问题重新分析。"
        insights = [summary]

    chart_sections = _chart_sections_from_llm_result(result, title, insights)
    if chart_sections:
        primary_section = chart_sections[0]
        columns = primary_section["columns"]
        chart_rows = primary_section["rows"]
        chart_spec = primary_section["chart"]
    else:
        columns, chart_rows, chart_spec = _chart_payload_from_llm_result(result, title, insights)

    intent_label = "report_generation" if any(key in effective_question for key in ("报告", "总结", "分析")) else "knowledge_qa"
    intent = IntentResult(intent_label, 0.9, "file_router", "上传文件触发文件专用分析")
    plan_steps = build_plan_steps(
        intent=intent,
        answer_type="knowledge_qa",
        execution_mode="document-llm",
        dataset_name=title,
        chart_type=chart_spec.get("type") or "none",
    )
    plan = ["解析上传文件", f"读取文件内容（{source_mode}）", "抽取可视化指标并生成图表", "围绕用户问题生成文件分析结论"]
    return {
        "session_id": session_id,
        "message": summary,
        "intent": "文档分析" if intent_label == "knowledge_qa" else "报告生成",
        "intent_label": intent_label,
        "intent_confidence": 0.9,
        "intent_method": "file_router",
        "intent_reason": "上传文件触发文件专用分析",
        "sql_source": None,
        "plan": plan,
        "plan_steps": plan_steps,
        "sql": "",
        "columns": columns,
        "rows": chart_rows,
        "chart": chart_spec,
        "chart_sections": chart_sections,
        "insights": insights,
        "knowledge_refs": [
            {
                "id": 0,
                "title": title,
                "content": cleaned_text[:1600],
                "category": str(result.get("document_type") or "uploaded_file"),
                "score": 1.0,
                "retrieval_mode": "uploaded-file",
            }
        ],
        "execution_mode": "document-llm",
        "answer_type": "knowledge_qa",
        "context_applied": False,
        "effective_question": effective_question,
    }
