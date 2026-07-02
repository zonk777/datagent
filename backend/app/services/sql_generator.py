from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import get_settings
from .security import UnsafeQueryError, validate_readonly_sql
from .field_aliases import alias_hints_for_question


class FieldNotFoundError(Exception):
    """Raised when the LLM determines that requested fields/metrics don't exist
    in the dataset schema — should NOT trigger a retry/repair loop."""

    def __init__(self, message: str, missing_fields: list[str] | None = None):
        self.message = message
        self.missing_fields = missing_fields or []
        super().__init__(message)


def needs_complex_sql(question: str) -> bool:
    """判断问题是否需要 LLM 生成 SQL（模板引擎无法覆盖）。

    覆盖场景：
    - 时间序列分析：同比/环比/移动平均/累计/窗口函数
    - 排名/筛选：TOP N、最高/最低、超过/大于/小于、HAVING
    - 多表/子查询：JOIN、子查询、UNION、EXISTS
    - 统计函数：中位数、标准差、方差、百分位
    - 高级分组：分组排名、每个分组内排序、GROUP_CONCAT
    """
    complex_terms = (
        # 时间序列 & 窗口
        "同比", "环比", "移动平均", "累计", "同比增长", "环比增长",
        "窗口", "rank", "row_number", "lag", "lead",
        "滚动", "滞后", "领先", "累加",
        # 排名 & 极值
        "最高", "最低", "top", "排名", "排行", "第几",
        "最多", "最少", "最大", "最小",
        # 条件筛选
        "超过", "大于", "小于", "高于", "低于", "不低于",
        "having",
        # 多表 & 子查询
        "join", "子查询", "union", "exists",
        # 占比 & 比率
        "占比", "占比变化", "比例", "比率",
        "前后", "对比上",
        # 高级聚合 & 统计
        "中位数", "标准差", "方差", "百分位", "众数",
        "每个.*第", "分组.*前",
    )
    text = question.lower()
    if any(term in text for term in complex_terms):
        return True
    # 额外模式检测：数字 + 排名相关词（如"前5名"、"TOP 10"）
    if re.search(r"(前|top|最[高低大])\s*\d+", text):
        return True
    if re.search(r"每个\S{0,6}(最高|最低|最大|最小|前|top)", text):
        return True
    return False


def _schema_payload(
    dataset: dict[str, Any],
    relevant_columns: set[str] | None = None,
) -> dict[str, Any]:
    """Build the schema description sent to the LLM.

    When ``relevant_columns`` is provided, each column gets a ``relevant``
    boolean flag so the LLM can distinguish columns that are actually needed
    for the metric from those that are merely available in the table.
    """
    columns = []
    for column in dataset.get("columns", []):
        col_info: dict[str, Any] = {
            "name": column["name"],
            "data_type": column.get("data_type", ""),
            "description": column.get("description", ""),
        }
        if relevant_columns is not None:
            col_info["relevant"] = column["name"] in relevant_columns
        columns.append(col_info)
    return {"table_name": dataset["table_name"], "columns": columns}


def _extract_relevant_columns(
    dataset: dict[str, Any],
    business_knowledge: list[dict] | None,
) -> set[str] | None:
    """Infer which table columns are referenced by the business knowledge entries.

    Strategy: for every column name in the dataset schema, check whether it
    appears (case-insensitively) anywhere in the title or content of the
    business knowledge chunks.  This handles both English column names
    (``conversions``) and Chinese descriptions (``转化人数`` → column with
    description ``转化人数``).
    """
    if not business_knowledge:
        return None

    # Build a single searchable text block from all knowledge entries.
    haystack_parts: list[str] = []
    for item in business_knowledge[:5]:
        for field in ("title", "content"):
            value = item.get(field, "")
            if isinstance(value, str):
                haystack_parts.append(value)
    haystack = " ".join(haystack_parts).lower()
    if not haystack:
        return None

    columns = dataset.get("columns", [])
    if not columns:
        return None

    relevant: set[str] = set()
    for col in columns:
        col_name = (col.get("name") or "").lower()
        col_desc = (col.get("description") or "").lower()
        # A column is "relevant" if its name or description appears in the
        # knowledge text.  This catches both ``conversions`` and ``转化人数``.
        if col_name and col_name in haystack:
            relevant.add(col["name"])
        elif col_desc and col_desc in haystack:
            relevant.add(col["name"])

    return relevant if relevant else None


def _strip_sql_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:sql)?\s*\n?", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\n?```\s*$", "", text).strip()
    return text


def _detect_extra_select_columns(
    sql: str,
    dataset_columns: set[str],
    relevant_columns: set[str] | None,
) -> set[str]:
    """Return column names that appear in SELECT but are *not* in ``relevant_columns``.

    Only fires when we have a known relevant-column set (i.e. business knowledge
    defined the metric).  The check is token-based rather than a full SQL parse
    — good enough to catch the common case where the LLM tacks on ``sales_amount``
    or ``profit`` to a metric query.
    """
    if not relevant_columns:
        return set()

    # Extract SELECT … FROM segment.
    m = re.search(r"\bSELECT\b\s+(.*?)\s+\bFROM\b", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return set()
    select_part = m.group(1)

    # Remove string literals so column-name-like strings inside quotes don't
    # cause false positives.
    select_part = re.sub(r"'[^']*'", "", select_part)
    select_part = re.sub(r'"[^"]*"', "", select_part)

    # Collect every identifier token in the SELECT clause.
    tokens = set(re.findall(r"[a-zA-Z_]\w*", select_part))
    sql_keywords = {
        "as", "distinct", "all", "case", "when", "then", "else", "end",
        "null", "not", "and", "or", "in", "between", "like", "is",
        "true", "false", "select", "from", "where", "group", "order",
        "having", "limit", "by", "asc", "desc", "on", "join", "left",
        "right", "inner", "outer", "full", "cross", "union", "cast",
        "coalesce", "nullif", "count", "sum", "avg", "min", "max",
        "round", "abs", "ceil", "floor", "year", "month", "day",
        "date", "strftime", "date_format", "if", "ifnull",
    }

    extra: set[str] = set()
    for token in tokens:
        token_lower = token.lower()
        if token_lower in sql_keywords:
            continue
        # Check if this token matches a dataset column that is NOT relevant.
        for col in dataset_columns:
            if col.lower() == token_lower and col not in relevant_columns:
                extra.add(col)
    return extra


def _sql_messages(
    question: str,
    dataset: dict[str, Any],
    limit: int,
    *,
    failed_sql: str | None = None,
    error: str | None = None,
    business_knowledge: list[dict] | None = None,
    intent_reason: str = "",
) -> list[dict[str, str]]:
    relevant_columns = _extract_relevant_columns(dataset, business_knowledge)

    payload: dict[str, Any] = {
        "question": question,
        "schema": _schema_payload(dataset, relevant_columns),
        "limit": limit,
    }
    alias_hints = alias_hints_for_question(question, dataset.get("columns", []))
    if alias_hints:
        payload["field_alias_hints"] = alias_hints
    if failed_sql or error:
        payload["repair_task"] = {
            "failed_sql": failed_sql or "",
            "error": error or "",
            "instruction": "请根据错误信息修正 SQL。仍然只能输出一条只读 SELECT/WITH 查询。",
        }
    if intent_reason:
        payload["intent_context"] = intent_reason
    if business_knowledge:
        payload["business_knowledge"] = [
            {
                "title": item.get("title", ""),
                "content": item.get("content", ""),
                "category": item.get("category", ""),
            }
            for item in business_knowledge[:5]
        ]

    # ── knowledge hint ──────────────────────────────────────────────
    knowledge_hint = ""
    if business_knowledge:
        knowledge_hint = (
            "请优先使用 business_knowledge 中的指标口径/计算公式生成 SQL。"
            "例如投诉率 = 投诉数/订单数、转化率 = 转化数/访问数等。"
        )

    # ── column constraint hint (CRITICAL) ───────────────────────────
    column_hint = ""
    if relevant_columns:
        col_list = "、".join(sorted(relevant_columns))
        column_hint = (
            f"列选择约束（极其重要）：schema 中标记 relevant=true 的列（{col_list}）"
            "是业务知识中定义该指标所需的列。你必须只 SELECT 这些相关列来计算指标。"
            "绝对不要为了「提供更多信息」而额外 SELECT 标记为 relevant=false 的列"
            "（如 sales_amount、profit 等无关列）。"
            "多余列会导致分析结果错误、图表展示混乱。"
        )
    else:
        # Even without explicit business knowledge, constrain the LLM.
        column_hint = (
            "列选择约束（极其重要）：只 SELECT 与用户问题直接相关的列。"
            "不要为了「提供上下文」或「可能有用」而 SELECT 无关列。"
            "只输出回答问题所必需的最少列集合。"
        )

    intent_hint = ""
    if intent_reason:
        intent_hint = f"当前分析意图: {intent_reason}。请根据此意图选择合理的聚合方式。"

    alias_hint = ""
    if alias_hints:
        alias_hint = (
            "业务字段别名/近似匹配提示（极其重要）：payload.field_alias_hints 已给出用户口语词与 schema 字段的候选映射。"
            "如果 matched_columns 非空，请优先使用其中最匹配的字段生成 SQL，不要把该业务词判定为 FIELD_NOT_FOUND。"
            "confidence 不是 high 时也可以先采用候选字段，系统会在最终回答中说明这是近似字段理解，方便用户后续修正。"
        )

    # ── field-not-found rule (always present) ─────────────────────────
    # Build a quick summary of available columns for the error message.
    columns_summary = "、".join(
        f"{col['name']}({col.get('description', '')})" if col.get('description') else col['name']
        for col in dataset.get("columns", [])
    )
    field_not_found_hint = (
        "字段不存在处理（极其重要）："
        "如果用户问题中提到的指标/字段/列在当前 schema 中不存在，且 field_alias_hints 中也没有可用 matched_columns，"
        "绝对不要猜测、不要用相似列替代、不要编造 SQL。"
        f"直接输出: FIELD_NOT_FOUND: <用户提到的字段> 不存在于当前数据表。当前表可用列: {columns_summary}。"
    )

    return [
        {
            "role": "system",
            "content": (
                "你是安全 SQL 生成与修复器。只输出一条只读 SELECT/WITH SQL，不要解释，不要 Markdown。"
                "只能访问给定 table_name；禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE 等写入或管理语句；"
                f"结果必须 LIMIT {limit} 以内。"
                f"{column_hint}"
                f"{field_not_found_hint}"
                f"{alias_hint}{knowledge_hint}{intent_hint}"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


async def _request_llm_sql(
    question: str,
    dataset: dict[str, Any],
    limit: int,
    *,
    failed_sql: str | None = None,
    error: str | None = None,
    business_knowledge: list[dict] | None = None,
    intent_reason: str = "",
    extra_column_repair: bool = True,
) -> str:
    settings = get_settings()
    messages = _sql_messages(
        question, dataset, limit,
        failed_sql=failed_sql,
        error=error,
        business_knowledge=business_knowledge,
        intent_reason=intent_reason,
    )
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={"model": settings.llm_model, "temperature": 0, "messages": messages},
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

    # ── field-not-found detection ──────────────────────────────────────
    _raw = content.strip()
    _fnf_match = re.match(r"^FIELD_NOT_FOUND\s*:\s*(.+)", _raw, re.IGNORECASE)
    if _fnf_match:
        explanation = _fnf_match.group(1).strip()
        raise FieldNotFoundError(explanation)

    sql = _strip_sql_fences(content)
    if not re.search(r"\bLIMIT\s+\d+\b", sql, flags=re.IGNORECASE):
        sql = f"{sql.rstrip(';')} LIMIT {limit}"

    # ── extra-column defense check ────────────────────────────────────
    if extra_column_repair and business_knowledge:
        relevant_columns = _extract_relevant_columns(dataset, business_knowledge)
        if relevant_columns:
            dataset_cols = {col["name"] for col in dataset.get("columns", [])}
            extra = _detect_extra_select_columns(sql, dataset_cols, relevant_columns)
            if extra:
                # Auto-repair: tell the LLM to remove those extra columns.
                repair_extra_prompt = (
                    f"你的上一条 SQL 多查询了以下无关列: {', '.join(sorted(extra))}。"
                    "请重新生成 SQL，严格只 SELECT 计算该指标所需的列，去掉多余列。"
                )
                try:
                    repair_messages = [
                        messages[0],  # same system prompt
                        messages[1],  # original user message
                        {"role": "assistant", "content": sql},
                        {"role": "user", "content": repair_extra_prompt},
                    ]
                    async with httpx.AsyncClient(timeout=12) as client2:
                        repair_resp = await client2.post(
                            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                            json={"model": settings.llm_model, "temperature": 0, "messages": repair_messages},
                        )
                        repair_resp.raise_for_status()
                        repaired = _strip_sql_fences(repair_resp.json()["choices"][0]["message"]["content"])
                        if not re.search(r"\bLIMIT\s+\d+\b", repaired, flags=re.IGNORECASE):
                            repaired = f"{repaired.rstrip(';')} LIMIT {limit}"
                        sql = repaired
                except (httpx.HTTPError, KeyError, TypeError, ValueError):
                    pass  # Repair failed — use the original SQL (extra cols better than nothing).

    return validate_readonly_sql(sql, dataset["table_name"])


async def generate_llm_sql_with_repair(
    question: str,
    dataset: dict[str, Any],
    limit: int,
    *,
    max_attempts: int = 3,
    failed_sql: str | None = None,
    error: str | None = None,
    business_knowledge: list[dict] | None = None,
    intent_reason: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.llm_configured:
        return {
            "sql": None,
            "attempts": 0,
            "repair_attempts": 0,
            "repaired": False,
            "repair_success_rate": None,
            "errors": [],
        }

    errors: list[dict[str, Any]] = []
    current_failed_sql = failed_sql
    current_error = error
    seeded_repair = bool(failed_sql or error)
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            sql = await _request_llm_sql(
                question,
                dataset,
                limit,
                failed_sql=current_failed_sql,
                error=current_error,
                business_knowledge=business_knowledge,
                intent_reason=intent_reason,
            )
            repair_attempts = max(0, attempt - 1 + (1 if seeded_repair else 0))
            return {
                "sql": sql,
                "attempts": attempt,
                "repair_attempts": repair_attempts,
                "repaired": repair_attempts > 0,
                "repair_success_rate": 1.0 if repair_attempts else None,
                "errors": errors,
            }
        except FieldNotFoundError:
            raise  # Don't retry — the field genuinely doesn't exist.
        except (httpx.HTTPError, KeyError, TypeError, ValueError, UnsafeQueryError) as exc:
            current_error = str(exc)
            errors.append(
                {
                    "attempt": attempt,
                    "error": current_error,
                    "failed_sql": current_failed_sql,
                }
            )

    repair_attempts = max(0, len(errors) - 1 + (1 if seeded_repair else 0))
    return {
        "sql": None,
        "attempts": len(errors),
        "repair_attempts": repair_attempts,
        "repaired": False,
        "repair_success_rate": 0.0 if repair_attempts else None,
        "errors": errors,
    }


async def generate_llm_sql(
    question: str,
    dataset: dict[str, Any],
    limit: int,
    business_knowledge: list[dict] | None = None,
    intent_reason: str = "",
) -> str | None:
    result = await generate_llm_sql_with_repair(
        question, dataset, limit,
        business_knowledge=business_knowledge,
        intent_reason=intent_reason,
    )
    return result["sql"]


async def repair_llm_sql(
    question: str,
    dataset: dict[str, Any],
    limit: int,
    failed_sql: str,
    error: str,
    business_knowledge: list[dict] | None = None,
    intent_reason: str = "",
) -> str | None:
    result = await generate_llm_sql_with_repair(
        question,
        dataset,
        limit,
        max_attempts=1,
        failed_sql=failed_sql,
        error=error,
        business_knowledge=business_knowledge,
        intent_reason=intent_reason,
    )
    return result["sql"]


def query_plan_source(question: str, llm_sql: str | None) -> str:
    if llm_sql:
        return "llm_sql"
    return "template_sql_complex_fallback" if needs_complex_sql(question) else "template_sql"
