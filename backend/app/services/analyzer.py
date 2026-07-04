from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..config import get_settings
from ..db import connect, using_mysql
from .intent_classifier import IntentResult, classify_intent
from .chart_recommender import recommend_chart
from .knowledge import search_knowledge
from .llm import answer_from_knowledge, narrate_database_insights, patch_insights, polish_insights, reflect_on_insights
from .analysis_planner import plan_analysis
from .data_profiler import profile_dataset
from .document_analyzer import analyze_uploaded_document, wants_database_context
from .dimension_executor import execute_plan_stream
from .memory import compress_history, format_profile_context, llm_merge_followup, load_user_profile, update_user_profile_from_analysis
from .meta_router import route_intent
from .planner import build_plan_steps, plan_titles
from .semantic_cache import lookup as cache_lookup, store as cache_store
from .python_executor import execute_python_analysis
from .react_agent import run_react_loop
from .security import validate_readonly_sql
from .session_documents import (
    load_session_document_context,
    save_session_document,
    should_use_session_documents,
    update_session_document_summary,
)
from .sql_generator import FieldNotFoundError, generate_llm_sql, query_plan_source, repair_llm_sql
from .field_aliases import FIELD_ALIAS_GROUPS, field_mapping_notes, find_column_by_alias, normalize_alias_text, resolve_field_references


REGIONS = ["华东", "华南", "华北", "西南"]
MAX_CHART_SECTIONS = 6


@dataclass
class QueryPlan:
    sql: str
    params: list[Any]
    x_field: str
    y_field: str
    series_field: str | None
    series_fields: list[str]
    time_description: str | None
    field_mappings: list[dict[str, Any]] = field(default_factory=list)


def _dataset(dataset_id: int | None) -> dict:
    with connect() as conn:
        if dataset_id:
            row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM datasets ORDER BY id LIMIT 1").fetchone()
        columns = [] if not row else [
            dict(item)
            for item in conn.execute(
                "SELECT name, data_type, description, sample_value, null_rate FROM dataset_columns WHERE dataset_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
        ]
    if not row:
        raise ValueError("没有可用数据集，请先上传数据")
    result = dict(row)
    result["columns"] = columns
    return result


def _find_column(columns: list[dict], keywords: tuple[str, ...], numeric: bool | None = None) -> str | None:
    numeric_types = ("int", "float", "double", "decimal", "real", "number")
    for column in columns:
        haystack = f"{column['name']} {column.get('description', '')}".lower()
        is_numeric = any(token in column["data_type"].lower() for token in numeric_types)
        if numeric is not None and is_numeric != numeric:
            continue
        if numeric is True and _is_index_like_column_meta(column):
            continue
        if any(keyword.lower() in haystack for keyword in keywords):
            return column["name"]
    return None


def _is_numeric_type(data_type: Any) -> bool:
    return any(token in str(data_type or "").lower() for token in ("int", "float", "double", "decimal", "real", "number"))


def _is_index_like_column_meta(column: dict[str, Any]) -> bool:
    name = str(column.get("name") or "").strip().lower()
    desc = str(column.get("description") or "").strip().lower()
    compact = re.sub(r"[\s:_\-]+", "", f"{name} {desc}")
    if name in {"id", "index", "idx", "unnamed: 0", "unnamed__0", "rowid", "row_id"}:
        return True
    return any(token in compact for token in ("unnamed0", "rowindex", "rownumber", "序号", "编号", "索引"))


def _column_profile(columns: list[dict]) -> dict[str, str | None]:
    first_numeric = next(
        (item["name"] for item in columns if _is_numeric_type(item.get("data_type")) and not _is_index_like_column_meta(item)),
        None,
    )
    first_dimension = next(
        (item["name"] for item in columns if not _is_numeric_type(item.get("data_type"))),
        columns[0]["name"] if columns else None,
    )
    influencer_dimension = find_column_by_alias(columns, "达人", numeric=False) or find_column_by_alias(columns, "达人")
    customer_dimension = find_column_by_alias(columns, "客户", numeric=False) or _find_column(
        columns, ("customer", "client", "user", "member", "buyer", "客户", "用户", "会员", "买家"), False
    )
    movie_dimension = find_column_by_alias(columns, "电影", numeric=False) or _find_column(
        columns, ("movie", "film", "title", "movie_name", "film_name", "片名", "影片", "电影"), False
    )
    box_office_metric = find_column_by_alias(columns, "票房", numeric=True) or _find_column(
        columns, ("box_office", "boxoffice", "gross", "total_gross", "movie_gross", "票房"), True
    )
    sales_explicit = (
        _find_column(
            columns,
            (
                "sales_amount",
                "item_gmv",
                "gmv",
                "revenue",
                "item_paid_amount",
                "paid_amount",
                "net_paid",
                "销售额",
                "营收",
                "收入",
                "成交金额",
                "实付金额",
            ),
            True,
        )
        or _find_column(columns, ("amount", "金额"), True)
    )
    return {
        "date": _find_column(columns, ("date", "time", "日期", "时间")),
        "region": _find_column(columns, ("region", "area", "地区", "区域")),
        "product": _find_column(
            columns,
            ("product", "category", "genre", "type", "segment", "class", "产品", "品类", "类别", "类型", "题材", "曲风", "种类"),
            False,
        ),
        "channel": _find_column(columns, ("channel", "渠道")),
        "customer": customer_dimension,
        "influencer": influencer_dimension,
        "movie": movie_dimension,
        "box_office": box_office_metric,
        "sales_explicit": sales_explicit,
        "sales": sales_explicit or first_numeric,
        "orders": _find_column(columns, ("order_count", "orders", "订单数", "销量"), True),
        "profit": _find_column(columns, ("profit", "gross_profit", "net_profit", "利润", "毛利", "净利"), True),
        "complaints": _find_column(columns, ("complaint", "投诉"), True),
        "visits": _find_column(columns, ("visit", "traffic", "访问", "流量"), True),
        "conversions": _find_column(columns, ("conversion", "转化"), True),
        "refund": find_column_by_alias(columns, "退款", numeric=True),
        "status": _find_column(columns, ("settlement_status", "item_status", "status", "状态", "结算状态", "订单状态"), False),
        "receivables": find_column_by_alias(columns, "应收账款", numeric=True),
        "aging": find_column_by_alias(columns, "账龄"),
        "cash_flow": find_column_by_alias(columns, "现金流", numeric=True),
        "assets": find_column_by_alias(columns, "资产", numeric=True),
        "liabilities": find_column_by_alias(columns, "负债", numeric=True),
        "rd": find_column_by_alias(columns, "研发投入", numeric=True),
        "first_numeric": first_numeric,
        "first_dimension": first_dimension,
    }


def _metric(question: str, profile: dict[str, str | None], columns: list[dict]) -> tuple[str, str, str, list[dict[str, Any]]]:
    q_lower = question.lower()
    if "投诉" in question and profile["complaints"] and profile["orders"]:
        return (
            f"ROUND(100.0 * SUM({profile['complaints']}) / NULLIF(SUM({profile['orders']}), 0), 2)",
            "投诉率",
            "%",
            [],
        )
    if "转化" in question and profile["conversions"] and profile["visits"]:
        return (
            f"ROUND(100.0 * SUM({profile['conversions']}) / NULLIF(SUM({profile['visits']}), 0), 2)",
            "转化率",
            "%",
            [],
        )
    if "利润" in question and profile["profit"]:
        return (f"ROUND(SUM({profile['profit']}), 2)", "毛利润", "元", [])
    if ("订单" in question or "销量" in question) and profile["orders"]:
        return (f"SUM({profile['orders']})", "订单数", "单", [])
    if any(term in question for term in ("销售额", "销售", "营收", "收入", "成交金额")) and profile["sales"]:
        return (f"ROUND(SUM({profile['sales']}), 2)", "销售额", "元", [])
    if any(term in question for term in ("应收账款", "应收", "账款", "回款", "欠款")) and profile.get("receivables"):
        return (f"ROUND(SUM({profile['receivables']}), 2)", "应收账款", "元", [])
    if any(term in question for term in ("现金流", "经营现金流", "现金流量")) and profile.get("cash_flow"):
        return (f"ROUND(SUM({profile['cash_flow']}), 2)", "现金流", "元", [])
    if any(term in question for term in ("研发", "研发投入", "研发费用")) and profile.get("rd"):
        return (f"ROUND(SUM({profile['rd']}), 2)", "研发投入", "元", [])
    if any(term in question for term in ("资产", "总资产")) and profile.get("assets"):
        return (f"ROUND(SUM({profile['assets']}), 2)", "资产", "元", [])
    if any(term in question for term in ("负债", "债务")) and profile.get("liabilities"):
        return (f"ROUND(SUM({profile['liabilities']}), 2)", "负债", "元", [])
    if any(term in question for term in ("票房", "总票房", "累计票房")) or any(
        term in q_lower for term in ("box office", "box_office", "boxoffice", "gross")
    ):
        box_office = profile.get("box_office") or find_column_by_alias(columns, "票房", numeric=True)
        if box_office:
            return (
                f"ROUND(SUM({box_office}), 2)",
                "票房",
                "",
                [{"term": "票房", "column": box_office, "label": "票房", "score": 1.0, "reason": "business_alias"}],
            )
    if any(term in question for term in ("预算", "成本", "投入")) or any(term in q_lower for term in ("budget", "cost")):
        budget_metric = _find_column(
            columns,
            ("production_budget", "budget", "cost", "生产预算", "预算", "成本", "投入"),
            True,
        )
        if budget_metric:
            label = _safe_field_label(columns, budget_metric, "预算")
            return (
                f"ROUND(SUM({budget_metric}), 2)",
                label,
                "",
                [{"term": "预算", "column": budget_metric, "label": label, "score": 0.95, "reason": "budget_metric"}],
            )
    if any(term in question for term in ("受欢迎", "热门", "热度", "播放", "观看", "浏览", "流量")) or any(
        term in q_lower for term in ("popular", "popularity", "view", "views", "play", "plays")
    ):
        popularity_metric = (
            _find_column(columns, ("worldwide_gross", "global_gross", "worldwide", "全球票房"), True)
            or _find_column(columns, ("domestic_gross", "box_office", "boxoffice", "gross", "票房"), True)
            or _find_column(columns, ("play_count", "views", "view_count", "play", "播放量", "观看量", "浏览量", "热度", "流量"), True)
            or profile.get("box_office")
            or profile.get("visits")
            or profile.get("sales")
        )
        if popularity_metric:
            label = _safe_field_label(columns, popularity_metric, "热度")
            return (
                f"ROUND(SUM({popularity_metric}), 2)",
                label,
                "",
                [{"term": "受欢迎", "column": popularity_metric, "label": label, "score": 0.9, "reason": "popularity_metric"}],
            )
    dynamic_metric = resolve_field_references(question, columns, numeric=True, limit=1, min_score=0.72)
    if dynamic_metric:
        match = dynamic_metric[0]
        label = _safe_field_label(columns, str(match["column"]), "指标值")
        return (f"ROUND(SUM({match['column']}), 2)", label, "", dynamic_metric)
    selected = profile["sales"] or profile["first_numeric"]
    if not selected:
        raise ValueError("当前数据集没有可聚合的数值字段")
    label = "销售额" if profile.get("sales_explicit") == selected else _column_display_label(_column_by_name(columns, selected), str(selected))
    return (f"ROUND(SUM({selected}), 2)", label, "元" if label == "销售额" else "", [])


def _column_is_numeric_meta(column: dict[str, Any]) -> bool:
    return _is_numeric_type(column.get("data_type")) and not _is_index_like_column_meta(column)


def _column_is_time_meta(column: dict[str, Any]) -> bool:
    text = f"{column.get('name', '')} {column.get('description', '')} {column.get('data_type', '')}".lower()
    return any(token in text for token in ("date", "time", "日期", "时间", "月份", "year", "month", "day"))


def _column_display_label(column: dict[str, Any] | None, fallback: str = "") -> str:
    if not column:
        return fallback
    desc = str(column.get("description") or "").strip()
    name = str(column.get("name") or "").strip()
    return desc or name or fallback


def _safe_field_label(columns: list[dict[str, Any]], column_name: str | None, fallback: str = "字段") -> str:
    """Use stable dataset metadata as display/query alias, never a full user sentence."""
    label = _column_display_label(_column_by_name(columns, column_name), str(column_name or fallback)).strip()
    if not label:
        label = fallback
    # A full natural-language question is a bad SQL alias and can collide with other aliases.
    if len(label) > 18:
        label = str(column_name or fallback)
    return label


def _unique_alias(label: str, used: set[str], suffix: str) -> str:
    base = str(label or suffix).strip() or suffix
    if base not in used:
        used.add(base)
        return base
    candidate = f"{base}{suffix}"
    index = 2
    while candidate in used:
        candidate = f"{base}{suffix}{index}"
        index += 1
    used.add(candidate)
    return candidate


def _column_by_name(columns: list[dict[str, Any]], name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    return next((column for column in columns if str(column.get("name")) == str(name)), None)


def _looks_like_table_overview(question: str) -> bool:
    q = question.lower()
    overview_terms = (
        "描述这个表", "描述一下表", "介绍这个表", "介绍一下表", "这个表", "这张表",
        "表结构", "字段结构", "有哪些字段", "字段含义", "数据概览", "整体概览",
        "数据集概览", "表的内容", "表内容", "样例数据", "预览数据", "schema",
        "目前数据库", "当前数据库", "这个数据库", "数据库是关于什么", "数据库关于什么",
        "当前数据源", "这个数据源", "数据源是关于什么", "数据源关于什么", "当前数据集",
        "是什么内容", "什么内容", "有什么内容", "有哪些内容",
        "describe table", "describe the table", "profile", "overview",
    )
    if any(term in q for term in overview_terms):
        return True
    data_object_terms = ("表", "数据集", "数据源", "数据库", "业务库", "数据表")
    meta_terms = ("描述", "介绍", "概览", "关于什么", "是什么", "什么内容", "有哪些", "字段")
    return any(term in question for term in meta_terms) and any(term in question for term in data_object_terms)


def _extract_top_n(question: str, default: int = 10) -> int:
    patterns = (
        r"(?:top|TOP)\s*([0-9]{1,3})",
        r"前\s*([0-9]{1,3})",
        r"([0-9]{1,3})\s*(?:个|名|条)?.{0,4}(?:最高|最大|最多|排行|排名)",
    )
    for pattern in patterns:
        match = re.search(pattern, question)
        if match:
            return max(1, min(int(match.group(1)), 100))
    return default


def _looks_like_generic_topn(question: str) -> bool:
    q = question.lower()
    complex_terms = ("投诉率", "转化率", "同比", "环比", "趋势", "归因", "异常", "占比", "率")
    if any(term in question for term in complex_terms):
        return False
    rank_terms = ("top", "前", "排名", "排行", "榜", "最高", "最大", "最多", "topn")
    if any(term in q for term in rank_terms):
        return True
    return "票房" in question and any(term in question for term in ("电影", "影片", "名称"))


def _best_generic_metric(question: str, columns: list[dict[str, Any]], profile: dict[str, str | None]) -> tuple[str | None, str, list[dict[str, Any]]]:
    q_lower = question.lower()
    if "票房" in question or any(term in q_lower for term in ("box office", "box_office", "boxoffice", "gross")):
        column = profile.get("box_office") or find_column_by_alias(columns, "票房", numeric=True)
        if column:
            return column, "票房", [{"term": "票房", "column": column, "label": "票房", "score": 1.0, "reason": "business_alias"}]
    if any(term in question for term in ("预算", "成本", "投入")) or any(term in q_lower for term in ("budget", "cost")):
        column = _find_column(columns, ("production_budget", "budget", "cost", "生产预算", "预算", "成本", "投入"), True)
        if column:
            label = _safe_field_label(columns, column, "预算")
            return column, label, [{"term": "预算", "column": column, "label": label, "score": 0.95, "reason": "budget_metric"}]
    if any(term in question for term in ("受欢迎", "热门", "热度", "播放", "观看", "浏览", "流量")) or any(
        term in q_lower for term in ("popular", "popularity", "view", "views", "play", "plays")
    ):
        column = (
            _find_column(columns, ("worldwide_gross", "global_gross", "worldwide", "全球票房"), True)
            or _find_column(columns, ("domestic_gross", "box_office", "boxoffice", "gross", "票房"), True)
            or _find_column(columns, ("play_count", "views", "view_count", "play", "播放量", "观看量", "浏览量", "热度", "流量"), True)
            or profile.get("box_office")
            or profile.get("visits")
            or profile.get("sales")
        )
        if column:
            label = _safe_field_label(columns, column, "热度")
            return column, label, [{"term": "受欢迎", "column": column, "label": label, "score": 0.9, "reason": "popularity_metric"}]
    dynamic = resolve_field_references(question, columns, numeric=True, limit=1, min_score=0.50)
    if dynamic:
        match = dynamic[0]
        column_name = str(match["column"])
        if _is_index_like_column_meta(_column_by_name(columns, column_name) or {}):
            dynamic = []
        else:
            return column_name, _safe_field_label(columns, column_name, "指标值"), dynamic
    selected = profile.get("sales_explicit") or profile.get("sales") or profile.get("first_numeric")
    if not selected:
        return None, "数值", []
    if selected == profile.get("box_office"):
        label = "票房"
    elif selected == profile.get("sales_explicit"):
        label = "销售额"
    else:
        label = _column_display_label(_column_by_name(columns, selected), str(selected))
    mappings = []
    if selected == profile.get("first_numeric") and selected != profile.get("sales_explicit") and selected != profile.get("box_office"):
        mappings.append({
            "term": "默认数值指标",
            "column": selected,
            "label": label,
            "score": 0.45,
            "reason": "fallback_numeric",
            "note": "未明确识别到用户指定指标，已先选择最接近的数值字段，可继续追问指定其它字段。",
        })
    return selected, label, mappings


def _best_generic_dimension(question: str, columns: list[dict[str, Any]], profile: dict[str, str | None]) -> tuple[str | None, str, list[dict[str, Any]]]:
    q_lower = question.lower()
    if any(term in question for term in ("哪种", "哪类", "类型", "题材", "类别", "种类", "曲风")) or any(
        term in q_lower for term in ("genre", "type", "category")
    ):
        column = profile.get("product")
        if column and column != profile.get("date"):
            return column, _safe_field_label(columns, column, "类型"), [
                {"term": "类型", "column": column, "label": _safe_field_label(columns, column, "类型"), "score": 0.9, "reason": "category_dimension"}
            ]
    if any(term in question for term in ("电影", "影片", "片名", "票房")) or any(
        term in q_lower for term in ("movie", "film", "box office", "box_office", "boxoffice")
    ):
        column = profile.get("movie") or find_column_by_alias(columns, "电影", numeric=False)
        if column:
            return column, "电影", [{"term": "电影", "column": column, "label": "电影", "score": 1.0, "reason": "business_alias"}]
    dynamic = resolve_field_references(question, columns, numeric=False, limit=1, min_score=0.50)
    if dynamic:
        match = dynamic[0]
        column_name = str(match["column"])
        if column_name != profile.get("date"):
            return column_name, _safe_field_label(columns, column_name, "维度"), dynamic
    for key, label in (("product", "产品"), ("customer", "客户"), ("region", "地区"), ("channel", "渠道"), ("first_dimension", "维度")):
        column_name = profile.get(key)
        if column_name and column_name != profile.get("date"):
            return column_name, label if label != "维度" else _column_display_label(_column_by_name(columns, column_name), str(column_name)), []
    return None, "维度", []


def _run_table_overview(dataset: dict, limit: int) -> dict[str, Any]:
    table_name = dataset["table_name"]
    columns = dataset["columns"]
    row_limit = max(1, min(limit, 5))
    count_sql = validate_readonly_sql(f'SELECT COUNT(*) AS "总行数" FROM {table_name}', table_name)
    sample_sql = validate_readonly_sql(f"SELECT * FROM {table_name} LIMIT {row_limit}", table_name)
    with connect() as conn:
        count_row = dict(conn.execute(count_sql).fetchone() or {})
        sample_rows = [_jsonable_row(dict(row)) for row in conn.execute(sample_sql).fetchall()]

    numeric_columns = [column for column in columns if _column_is_numeric_meta(column)][:8]
    dimension_columns = [column for column in columns if not _column_is_numeric_meta(column)][:8]
    stats_by_column: dict[str, dict[str, Any]] = {}
    executed_sql = [count_sql, sample_sql]
    with connect() as conn:
        for column in numeric_columns:
            name = column["name"]
            stats_sql = validate_readonly_sql(
                f'SELECT ROUND(MIN({name}), 2) AS "最小值", ROUND(MAX({name}), 2) AS "最大值", '
                f'ROUND(AVG({name}), 2) AS "平均值", ROUND(SUM({name}), 2) AS "合计值" FROM {table_name}',
                table_name,
            )
            stats_by_column[name] = dict(conn.execute(stats_sql).fetchone() or {})
            executed_sql.append(stats_sql)
        for column in dimension_columns:
            name = column["name"]
            distinct_sql = validate_readonly_sql(f'SELECT COUNT(DISTINCT {name}) AS "不同值数量" FROM {table_name}', table_name)
            stats_by_column[name] = dict(conn.execute(distinct_sql).fetchone() or {})
            executed_sql.append(distinct_sql)

    rows: list[dict[str, Any]] = []
    for column in columns:
        name = column["name"]
        if _column_is_time_meta(column):
            role = "时间字段"
        elif _column_is_numeric_meta(column):
            role = "数值指标"
        else:
            role = "分类维度"
        overview = ""
        stats = stats_by_column.get(name) or {}
        if "合计值" in stats:
            overview = f"最小 {stats.get('最小值')}，最大 {stats.get('最大值')}，平均 {stats.get('平均值')}，合计 {stats.get('合计值')}"
        elif "不同值数量" in stats:
            overview = f"不同值数量 {stats.get('不同值数量')}"
        rows.append({
            "字段": name,
            "类型": column.get("data_type", ""),
            "角色": role,
            "业务说明": column.get("description") or "",
            "样例值": column.get("sample_value") or "",
            "空值率": column.get("null_rate", 0),
            "概览": overview,
        })

    numeric_count = len([column for column in columns if _column_is_numeric_meta(column)])
    dimension_count = len(columns) - numeric_count
    insights = [
        f"数据集「{dataset['name']}」对应物理表 {table_name}，共有 {count_row.get('总行数', dataset.get('row_count', 0))} 行、{len(columns)} 个字段。",
        f"字段结构上包含 {dimension_count} 个分类/文本/时间类字段、{numeric_count} 个数值指标字段，适合做字段理解、分组排名、趋势和统计概览。",
    ]
    if sample_rows:
        insights.append(f"已读取前 {len(sample_rows)} 行样例数据，可用于判断字段含义和后续分析方向。")
    plan = QueryPlan(
        sql="\n\n".join(executed_sql[:12]),
        params=[],
        x_field="字段",
        y_field="概览",
        series_field=None,
        series_fields=[],
        time_description=None,
    )
    return {
        "mode": "table_overview",
        "query_plan": plan,
        "rows": rows,
        "columns": list(rows[0].keys()) if rows else [],
        "chart": {
            "type": "none",
            "title": f"{dataset['name']} - 表结构与数据概览",
            "x_field": "字段",
            "y_field": "概览",
            "series_name": None,
            "series_field": None,
            "series_fields": [],
        },
        "insights": insights,
        "knowledge_refs": [{
            "id": 0,
            "title": f"{dataset['name']} 表结构",
            "content": "\n".join(f"{row['字段']} ({row['类型']}): {row['业务说明'] or row['角色']}" for row in rows[:30]),
            "category": "dataset_schema",
            "score": 1.0,
            "retrieval_mode": "schema-profile",
        }],
        "sample_rows": sample_rows,
        "plan_source": "schema_explorer",
    }


def _run_generic_topn(question: str, dataset: dict, limit: int) -> dict[str, Any] | None:
    profile = _column_profile(dataset["columns"])
    metric_column, metric_label, metric_mappings = _best_generic_metric(question, dataset["columns"], profile)
    dimension_column, dimension_label, dimension_mappings = _best_generic_dimension(question, dataset["columns"], profile)
    if not metric_column or not dimension_column:
        return None
    used_aliases: set[str] = set()
    dimension_label = _unique_alias(dimension_label, used_aliases, "维度")
    metric_label = _unique_alias(metric_label, used_aliases, "指标")
    top_n = _extract_top_n(question, 10)
    row_limit = max(1, min(top_n, limit, 100))
    table_name = dataset["table_name"]
    descending = not any(term in question for term in ("最低", "最少", "最小", "倒数", "后"))
    direction = "DESC" if descending else "ASC"
    sql = (
        f'SELECT {dimension_column} AS "{dimension_label}", '
        f'ROUND(SUM({metric_column}), 2) AS "{metric_label}" '
        f"FROM {table_name} "
        f"WHERE {dimension_column} IS NOT NULL AND {dimension_column} != '' "
        f"GROUP BY {dimension_column} ORDER BY 2 {direction} LIMIT {row_limit}"
    )
    safe_sql = validate_readonly_sql(sql, table_name)
    with connect() as conn:
        rows = [_jsonable_row(dict(row)) for row in conn.execute(safe_sql).fetchall()]
    rank_word = "最高" if descending else "最低"
    plan = QueryPlan(
        sql=safe_sql,
        params=[],
        x_field=dimension_label,
        y_field=metric_label,
        series_field=None,
        series_fields=[],
        time_description=None,
        field_mappings=[*dimension_mappings, *metric_mappings],
    )
    if not rows:
        return {
            "mode": "generic_topn",
            "query_plan": plan,
            "rows": [],
            "columns": [dimension_label, metric_label],
            "chart": {
                "type": "none",
                "title": f"{dataset['name']} - {metric_label} TOP{row_limit}",
                "x_field": dimension_label,
                "y_field": metric_label,
                "series_name": metric_label,
                "series_field": None,
                "series_fields": [],
            },
            "insights": [
                f"已按「{dimension_label}」汇总「{metric_label}」并查询 TOP{row_limit}，但当前筛选条件下没有查到可展示的数据。",
                "建议检查数据源是否为空、该字段是否存在空值，或换一个时间范围/分组维度继续追问。",
            ],
            "knowledge_refs": [],
            "plan_source": "generic_topn",
        }
    leader = rows[0]
    insights = [
        f"已按「{dimension_label}」聚合「{metric_label}」，返回 {row_limit} 条{rank_word}排名结果。",
        f"{rank_word}的是「{leader.get(dimension_label)}」，{metric_label}为 {leader.get(metric_label)}。",
        "说明：如果原表是一行一部电影/一个对象，结果等同于单条记录排名；如果同一对象有多行记录，则这里按对象汇总后排名。",
    ]
    for note in field_mapping_notes(plan.field_mappings):
        if note not in insights:
            insights.insert(0, note)
    for mapping in plan.field_mappings:
        note = mapping.get("note")
        if note and note not in insights:
            insights.insert(0, str(note))
    recommendation = recommend_chart(question, "data_query", rows, dimension_label, metric_label, [])
    chart = {
        "type": recommendation.get("type", "bar"),
        "title": f"{dataset['name']} - {metric_label} TOP{row_limit}",
        "x_field": dimension_label,
        "y_field": metric_label,
        "series_name": metric_label,
        "series_field": None,
        "series_fields": [],
        "recommendation": recommendation,
        "alternatives": recommendation.get("alternatives", []),
        "display_mode": recommendation.get("display_mode", "single"),
        "secondary_y_field": recommendation.get("secondary_y_field"),
        "facet_fields": recommendation.get("facet_fields", []),
    }
    return {
        "mode": "generic_topn",
        "query_plan": plan,
        "rows": rows,
        "columns": list(rows[0].keys()) if rows else [dimension_label, metric_label],
        "chart": chart,
        "insights": insights,
        "knowledge_refs": [],
        "plan_source": "generic_topn",
    }


def _question_mentions_database_subject(question: str) -> bool:
    compact = re.sub(r"\s+", "", question.lower())
    data_terms = (
        "数据库", "数据源", "数据集", "数据表", "业务库", "当前库", "当前表", "当前数据",
        "这个库", "这个表", "这张表", "database", "dataset", "table",
    )
    subject_terms = (
        "关于什么", "是什么内容", "什么内容", "有什么内容", "有哪些内容", "值得关注",
        "看一下", "看看", "分析一下", "整体分析", "完整分析", "全面分析", "多维度",
        "多角度", "综合分析", "关键指标", "核心指标", "业务情况", "画像",
        "profile", "overview", "describe",
    )
    return any(term.lower() in compact for term in data_terms) and any(term.lower() in compact for term in subject_terms)


def _wants_database_intelligence(question: str, intent_label: str | None = None) -> bool:
    if intent_label == "knowledge_qa":
        return False
    broad_terms = (
        "完整分析", "全面分析", "多维度", "多角度", "综合分析", "分析报告", "完整报告",
        "整体分析", "智能分析", "经营分析", "业务分析", "数据画像", "关键指标",
        "核心指标", "有什么值得关注", "帮我看看", "看一下这个数据", "看看这个数据",
        "分析当前数据库", "分析当前数据源", "分析这个数据库", "分析这个数据源",
        "客户集中度", "应收账款", "账龄", "回款风险", "经营风险", "财务风险",
        "收入结构", "产品结构", "渠道结构", "达人贡献", "头部依赖",
    )
    if any(term in question for term in broad_terms):
        return True
    return _question_mentions_database_subject(question)


def _should_force_database_analysis(question: str) -> bool:
    """Avoid routing dataset overview questions to knowledge QA by mistake."""
    compact = re.sub(r"\s+", "", question.lower())
    explicit_dataset_terms = (
        "数据库", "数据源", "数据集", "数据表", "业务库", "当前库", "当前表",
        "当前数据", "这张表", "这个表", "这个库", "database", "dataset", "table",
    )
    if _question_mentions_database_subject(question):
        return True
    if any(term.lower() in compact for term in explicit_dataset_terms) and _wants_database_intelligence(question):
        return True
    return False


def _column_text(column: dict[str, Any]) -> str:
    return f"{column.get('name', '')} {column.get('description', '')}".lower()


def _column_label(column: dict[str, Any] | None, fallback: str = "") -> str:
    if not column:
        return fallback
    return str(column.get("description") or column.get("name") or fallback).strip()


def _profile_column(dataset: dict, name: str | None) -> dict[str, Any] | None:
    return _column_by_name(dataset.get("columns") or [], name)


def _metric_label_from_profile(dataset: dict, profile: dict[str, str | None], metric: str | None) -> str:
    if not metric:
        return "指标值"
    if metric == profile.get("box_office"):
        return "票房"
    if metric == profile.get("sales_explicit"):
        return "销售额"
    if metric == profile.get("orders"):
        return "订单数"
    if metric == profile.get("profit"):
        return "利润"
    if metric == profile.get("receivables"):
        return "应收账款"
    if metric == profile.get("cash_flow"):
        return "现金流"
    if metric == profile.get("assets"):
        return "资产"
    if metric == profile.get("liabilities"):
        return "负债"
    if metric == profile.get("rd"):
        return "研发投入"
    return _column_label(_profile_column(dataset, metric), str(metric))


def _primary_metric(dataset: dict, profile: dict[str, str | None]) -> tuple[str | None, str]:
    for key in ("box_office", "sales_explicit", "profit", "orders", "receivables", "cash_flow", "assets", "rd", "first_numeric"):
        metric = profile.get(key)
        if metric:
            return metric, _metric_label_from_profile(dataset, profile, metric)
    return None, "指标值"


def _numeric_metric_candidates(dataset: dict, profile: dict[str, str | None], limit: int = 8) -> list[tuple[str, str]]:
    preferred_keys = ("box_office", "sales_explicit", "profit", "orders", "receivables", "cash_flow", "assets", "liabilities", "rd", "refund", "visits", "conversions")
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key in preferred_keys:
        column = profile.get(key)
        if column and column not in seen:
            candidates.append((column, _metric_label_from_profile(dataset, profile, column)))
            seen.add(column)
    for column in dataset.get("columns") or []:
        name = str(column.get("name") or "")
        if name and name not in seen and _column_is_numeric_meta(column):
            candidates.append((name, _column_label(column, name)))
            seen.add(name)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _dimension_candidates(dataset: dict, profile: dict[str, str | None], limit: int = 10) -> list[tuple[str, str, str]]:
    preferred = (
        ("movie", "电影", "按电影/影片观察头部贡献和长尾分布。"),
        ("product", "产品结构", "按产品/品类拆分，观察主要业务来源和结构差异。"),
        ("customer", "客户集中度", "按客户贡献度观察是否存在大客户依赖或收入集中风险。"),
        ("region", "区域分布", "按地区拆分，观察区域经营差异。"),
        ("channel", "渠道贡献", "按渠道拆分，观察不同来源的贡献度。"),
        ("influencer", "达人贡献", "按达人/主播/创作者拆分，观察头部贡献和依赖度。"),
        ("aging", "账龄结构", "按账龄/逾期维度观察回款风险。"),
        ("status", "状态分布", "按状态字段观察订单、结算或业务流程分布。"),
    )
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for key, label, description in preferred:
        column = profile.get(key)
        if column and column not in seen and column != profile.get("date"):
            candidates.append((column, label, description))
            seen.add(column)
    for column in dataset.get("columns") or []:
        name = str(column.get("name") or "")
        if not name or name in seen or name == profile.get("date"):
            continue
        if _column_is_numeric_meta(column) or _column_is_time_meta(column):
            continue
        text = _column_text(column)
        if any(token in text for token in ("id", "uuid", "编号", "编码")) and not any(token in text for token in ("name", "名称", "类型", "类别")):
            continue
        label = _column_label(column, name)
        candidates.append((name, label, f"按「{label}」拆分，观察不同类别的贡献差异。"))
        seen.add(name)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _infer_dataset_domain(dataset: dict, profile: dict[str, str | None]) -> tuple[str, list[str]]:
    basis: list[str] = []
    if profile.get("movie") or profile.get("box_office"):
        if profile.get("movie"):
            basis.append(f"电影字段：{profile['movie']}")
        if profile.get("box_office"):
            basis.append(f"票房字段：{profile['box_office']}")
        return "电影票房 / 作品表现统计", basis
    if profile.get("influencer") and (profile.get("sales_explicit") or profile.get("orders")):
        basis.extend([f"达人字段：{profile['influencer']}"])
        if profile.get("sales_explicit"):
            basis.append(f"交易指标：{profile['sales_explicit']}")
        return "直播电商 / 达人带货经营数据", basis
    if profile.get("customer") and (profile.get("receivables") or profile.get("aging")):
        basis.append(f"客户字段：{profile['customer']}")
        if profile.get("receivables"):
            basis.append(f"应收字段：{profile['receivables']}")
        if profile.get("aging"):
            basis.append(f"账龄字段：{profile['aging']}")
        return "客户经营 / 回款与账龄风险数据", basis
    if any(profile.get(key) for key in ("cash_flow", "assets", "liabilities", "rd", "profit")):
        for key, label in (("cash_flow", "现金流"), ("assets", "资产"), ("liabilities", "负债"), ("rd", "研发"), ("profit", "利润")):
            if profile.get(key):
                basis.append(f"{label}字段：{profile[key]}")
        return "企业财务 / 经营指标数据", basis
    if profile.get("sales_explicit") and any(profile.get(key) for key in ("product", "region", "channel", "customer")):
        basis.append(f"销售指标：{profile['sales_explicit']}")
        for key, label in (("product", "产品"), ("region", "区域"), ("channel", "渠道"), ("customer", "客户")):
            if profile.get(key):
                basis.append(f"{label}维度：{profile[key]}")
        return "企业销售 / 经营分析数据", basis
    numeric_count = len([column for column in dataset.get("columns") or [] if _column_is_numeric_meta(column)])
    dimension_count = len(dataset.get("columns") or []) - numeric_count
    basis.append(f"{dimension_count} 个维度/文本字段，{numeric_count} 个数值字段")
    return "通用业务统计数据", basis


def _run_time_series_query(
    dataset: dict,
    *,
    date_column: str | None,
    metric_column: str | None,
    metric_label: str,
    limit: int,
) -> tuple[QueryPlan, list[dict[str, Any]]] | None:
    if not date_column or not metric_column:
        return None
    table_name = dataset["table_name"]
    period_expr = f"DATE_FORMAT({date_column}, '%Y-%m')" if using_mysql() else f"strftime('%Y-%m', {date_column})"
    sql = (
        f'SELECT {period_expr} AS "月份", ROUND(SUM({metric_column}), 2) AS "{metric_label}" '
        f"FROM {table_name} WHERE {date_column} IS NOT NULL "
        f"GROUP BY {period_expr} ORDER BY {period_expr} LIMIT {max(1, min(limit, 200))}"
    )
    try:
        safe_sql = validate_readonly_sql(sql, table_name)
        with connect() as conn:
            rows = [_jsonable_row(dict(row)) for row in conn.execute(safe_sql).fetchall()]
        rows = [row for row in rows if row.get("月份") not in (None, "")]
        if len(rows) < 2:
            return None
        return QueryPlan(safe_sql, [], "月份", metric_label, None, [], "按月"), rows
    except Exception:
        return None


def _run_metric_overview_query(dataset: dict, metrics: list[tuple[str, str]], limit: int) -> tuple[QueryPlan, list[dict[str, Any]]] | None:
    metrics = metrics[: max(1, min(limit, 8))]
    if not metrics:
        return None
    table_name = dataset["table_name"]
    selects = [
        f'SELECT \'{label.replace("\'", "\'\'")}\' AS "指标", ROUND(SUM({column}), 2) AS "合计值" FROM {table_name}'
        for column, label in metrics
    ]
    sql = " UNION ALL ".join(selects)
    try:
        safe_sql = validate_readonly_sql(sql, table_name)
        with connect() as conn:
            rows = [_jsonable_row(dict(row)) for row in conn.execute(safe_sql).fetchall()]
        rows = [row for row in rows if row.get("合计值") is not None]
        if not rows:
            return None
        return QueryPlan(safe_sql, [], "指标", "合计值", None, [], None), rows
    except Exception:
        return None


def _run_null_rate_query(dataset: dict) -> tuple[QueryPlan, list[dict[str, Any]]] | None:
    rows = []
    for column in dataset.get("columns") or []:
        try:
            null_rate = float(column.get("null_rate") or 0)
        except (TypeError, ValueError):
            null_rate = 0.0
        if null_rate > 0:
            rows.append({"字段": _column_label(column, str(column.get("name") or "")), "空值率": round(null_rate * 100, 2)})
    rows.sort(key=lambda item: float(item.get("空值率") or 0), reverse=True)
    if not rows:
        return None
    plan = QueryPlan("/* 基于字段元数据 dataset_columns.null_rate 生成 */ SELECT 字段, 空值率 FROM dataset_columns", [], "字段", "空值率", None, [], None)
    return plan, rows[:12]


def _section_from_query(
    *,
    dataset: dict,
    question: str,
    intent_label: str,
    section_id: str,
    title: str,
    description: str,
    result: tuple[QueryPlan, list[dict[str, Any]]] | None,
    preferred_chart: str | None = None,
) -> dict[str, Any] | None:
    if not result:
        return None
    plan, rows = result
    if not rows:
        return None
    chart = _chart_from_plan(
        question=f"{question} {title}",
        intent_label=intent_label,
        dataset_name=dataset["name"],
        rows=rows,
        query_plan=plan,
        title_prefix=title,
    )
    if preferred_chart:
        chart = _with_chart_type(chart, preferred_chart, f"{title}更适合用{preferred_chart}展示。")
    try:
        insights = _draft_insights(rows, plan.x_field, plan.y_field, plan.series_fields)[:3]
    except Exception:
        insights = []
    return _chart_section(
        section_id=section_id,
        title=title,
        description=description,
        rows=rows[:200],
        chart=chart,
        insights=insights,
    )


def _run_database_intelligence(question: str, dataset: dict, limit: int, intent_label: str = "data_query") -> dict[str, Any] | None:
    profile = _column_profile(dataset["columns"])
    primary_metric, primary_metric_label = _primary_metric(dataset, profile)
    metrics = _numeric_metric_candidates(dataset, profile, limit=8)
    dimensions = _dimension_candidates(dataset, profile, limit=12)
    domain, domain_basis = _infer_dataset_domain(dataset, profile)

    overview = _run_table_overview(dataset, limit)
    sections: list[dict[str, Any]] = []
    omitted: list[str] = []

    def add(section: dict[str, Any] | None) -> None:
        if not section:
            return
        title = str(section.get("title") or "")
        if any(existing.get("title") == title for existing in sections):
            return
        if len(sections) >= MAX_CHART_SECTIONS:
            if title and title not in omitted:
                omitted.append(title)
            return
        sections.append(section)

    add(
        _section_from_query(
            dataset=dataset,
            question=question,
            intent_label=intent_label,
            section_id="db-metric-overview",
            title="核心指标概览",
            description="自动汇总表中主要数值指标，先判断这张表可用于观察哪些核心量化指标。",
            result=_run_metric_overview_query(dataset, metrics, min(limit, 8)),
            preferred_chart="bar",
        )
    )
    add(
        _section_from_query(
            dataset=dataset,
            question=question,
            intent_label="trend_analysis",
            section_id="db-time-trend",
            title="时间趋势",
            description=f"按时间观察「{primary_metric_label}」变化，用于发现增长、回落或波动。",
            result=_run_time_series_query(
                dataset,
                date_column=profile.get("date"),
                metric_column=primary_metric,
                metric_label=primary_metric_label,
                limit=min(limit, 200),
            ),
            preferred_chart="line",
        )
    )

    dimension_metric = primary_metric
    dimension_metric_label = primary_metric_label
    if profile.get("aging") and profile.get("receivables"):
        dimension_metric = profile.get("receivables")
        dimension_metric_label = "应收账款"
    for index, (dimension_column, dimension_label, description) in enumerate(dimensions, 1):
        metric_for_dimension = dimension_metric
        metric_label = dimension_metric_label
        if dimension_column == profile.get("aging") and profile.get("receivables"):
            metric_for_dimension = profile.get("receivables")
            metric_label = "应收账款"
        if not metric_for_dimension:
            continue
        preferred_chart = "pie" if any(term in dimension_label for term in ("结构", "集中度", "分布")) and index <= 4 else "bar"
        title = dimension_label
        if dimension_column == profile.get("movie") and metric_label == "票房":
            title = "电影票房TOP"
        add(
            _section_from_query(
                dataset=dataset,
                question=question,
                intent_label=intent_label,
                section_id=f"db-dim-{index}",
                title=title,
                description=description,
                result=_run_group_query(
                    dataset,
                    dimension_column=dimension_column,
                    dimension_label=title,
                    metric_column=metric_for_dimension,
                    metric_label=metric_label,
                    limit=12,
                ),
                preferred_chart=preferred_chart,
            )
        )

    add(
        _section_from_query(
            dataset=dataset,
            question=question,
            intent_label=intent_label,
            section_id="db-data-quality",
            title="数据质量",
            description="按字段空值率观察数据质量，空值率较高的字段会影响后续分析可靠性。",
            result=_run_null_rate_query(dataset),
            preferred_chart="bar",
        )
    )

    if not sections:
        return overview

    first_section = sections[0]
    all_sql = []
    for section in sections:
        chart = section.get("chart") or {}
        # section 自身不保存 SQL，这里从前几个查询结果无法逐一追溯时，用概览 SQL 兜底。
        if chart.get("title"):
            continue
    plan = QueryPlan(
        sql=overview["query_plan"].sql,
        params=[],
        x_field=first_section["chart"].get("x_field") or (first_section["columns"][0] if first_section["columns"] else ""),
        y_field=first_section["chart"].get("y_field") or (first_section["columns"][-1] if first_section["columns"] else ""),
        series_field=None,
        series_fields=[],
        time_description=None,
    )

    dimension_names = "、".join(item[1] for item in dimensions[:6]) or "暂无明显分类维度"
    metric_names = "、".join(label for _, label in metrics[:6]) or "暂无明显数值指标"
    insights = [
        f"我判断当前数据源更像「{domain}」。判断依据：{('；'.join(domain_basis[:5]) if domain_basis else '字段结构和样例元数据')}。",
        f"数据规模：约 {dataset.get('row_count', 0)} 行，{len(dataset.get('columns') or [])} 个字段；可用指标包括：{metric_names}。",
        f"可分析维度包括：{dimension_names}。本次已优先生成 {len(sections)} 组最有价值图表。",
    ]
    if omitted:
        insights.append(
            f"图表数量限制：本次最多展示 {MAX_CHART_SECTIONS} 张图，已省略：{'、'.join(omitted[:8])}"
            + ("等。" if len(omitted) > 8 else "。")
        )
    for section in sections[:3]:
        for insight in section.get("insights") or []:
            if insight not in insights:
                insights.append(insight)
                break

    return {
        "mode": "database_intelligence",
        "message": "已完成数据库智能探索。",
        "query_plan": plan,
        "rows": first_section.get("rows") or [],
        "columns": first_section.get("columns") or [],
        "chart": first_section.get("chart") or overview.get("chart"),
        "chart_sections": sections,
        "omitted_chart_sections": omitted,
        "insights": insights,
        "knowledge_refs": overview.get("knowledge_refs") or [],
        "sample_rows": overview.get("sample_rows") or [],
        "plan_source": "database_intelligence",
        "domain": domain,
    }


def _try_generic_data_exploration(question: str, dataset: dict, limit: int) -> dict[str, Any] | None:
    if _wants_database_intelligence(question):
        return _run_database_intelligence(question, dataset, limit)
    if _looks_like_generic_topn(question):
        return _run_generic_topn(question, dataset, limit)
    if _looks_like_table_overview(question):
        return _run_table_overview(dataset, limit)
    return None


def _time_filter(question: str, date_column: str | None, table_name: str) -> tuple[list[str], list[Any], str | None]:
    if not date_column:
        return [], [], None
    match = re.search(r"(?:近|最近)\s*(\d+)\s*(天|日|周|个月|月)", question)
    if using_mysql():
        max_date = f"(SELECT MAX(DATE({date_column})) FROM {table_name})"
        if match:
            amount = max(1, int(match.group(1)))
            unit = match.group(2)
            if unit in ("天", "日"):
                return [f"DATE({date_column}) >= DATE_SUB({max_date}, INTERVAL ? DAY)"], [amount - 1], f"近{amount}天"
            if unit == "周":
                return [f"DATE({date_column}) >= DATE_SUB({max_date}, INTERVAL ? DAY)"], [amount * 7 - 1], f"近{amount}周"
            return [f"DATE({date_column}) >= DATE_SUB(DATE_FORMAT({max_date}, '%%Y-%%m-01'), INTERVAL ? MONTH)"], [amount - 1], f"近{amount}个月"
        if "本月" in question:
            return [f"DATE({date_column}) >= DATE_FORMAT({max_date}, '%%Y-%%m-01')"], [], "本月"
        if "今年" in question or "本年" in question:
            return [f"YEAR({date_column}) = YEAR({max_date})"], [], "本年"
        return [], [], None

    max_date = f"(SELECT MAX(date({date_column})) FROM {table_name})"
    if match:
        amount = max(1, int(match.group(1)))
        unit = match.group(2)
        if unit in ("天", "日"):
            return [f"date({date_column}) >= date({max_date}, ?)"], [f"-{amount - 1} days"], f"近{amount}天"
        if unit == "周":
            return [f"date({date_column}) >= date({max_date}, ?)"], [f"-{amount * 7 - 1} days"], f"近{amount}周"
        return [f"date({date_column}) >= date({max_date}, 'start of month', ?)"], [f"-{amount - 1} months"], f"近{amount}个月"
    if "本月" in question:
        return [f"date({date_column}) >= date({max_date}, 'start of month')"], [], "本月"
    if "今年" in question or "本年" in question:
        return [f"strftime('%Y', {date_column}) = strftime('%Y', {max_date})"], [], "本年"
    return [], [], None


def _breakdowns(question: str, profile: dict[str, str | None]) -> list[tuple[str, str]]:
    targets = [
        (("地区", "区域", "大区"), profile["region"], "区域"),
        (("产品", "展品", "品类", "类别", "类型", "题材", "曲风", "种类", "哪种", "哪类"), profile["product"], "产品类别"),
        (("渠道",), profile["channel"], "渠道"),
        (("客户", "用户", "会员", "买家", "消费者", "大客户", "客户集中度"), profile.get("customer"), "客户"),
        (("账龄", "逾期", "账期", "应收账款账龄"), profile.get("aging"), "账龄"),
        (("达人", "主播", "kol", "koc", "博主", "网红", "创作者", "influencer", "creator", "talent"), profile["influencer"], "达人"),
    ]
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for keywords, column, label in targets:
        if column and any(keyword in question for keyword in keywords):
            if column not in seen:
                result.append((column, label))
                seen.add(column)
    return result


def _build_query(question: str, dataset: dict, limit: int) -> QueryPlan:
    profile = _column_profile(dataset["columns"])
    metric_sql, metric_label, _, field_mappings = _metric(question, profile, dataset["columns"])
    table_name = dataset["table_name"]
    filters, params, time_description = _time_filter(question, profile["date"], table_name)
    for region in REGIONS:
        if region in question and profile["region"]:
            filters.append(f"{profile['region']} = ?")
            params.append(region)

    breakdowns = _breakdowns(question, profile)
    seen_breakdown_columns = {column for column, _ in breakdowns}
    seen_breakdown_labels = {label for _, label in breakdowns}
    for match in resolve_field_references(question, dataset["columns"], numeric=False, limit=3, min_score=0.66):
        column = match["column"]
        label = _safe_field_label(dataset["columns"], str(column), "维度")
        if (
            column == profile.get("movie")
            and profile.get("product")
            and profile.get("product") in seen_breakdown_columns
            and any(term in question for term in ("哪种", "哪类", "类型", "题材", "类别", "种类"))
        ):
            # “哪种电影”里的“电影”是业务对象，不是要求按片名再拆一层；
            # 否则会把电影名误当成分组/指标，导致后续图表和洞察不稳定。
            continue
        if column in seen_breakdown_columns:
            if not any(item.get("column") == column and item.get("term") == match.get("term") for item in field_mappings):
                field_mappings.append(match)
            continue
        if label in seen_breakdown_labels:
            if not any(item.get("column") == column and item.get("term") == match.get("term") for item in field_mappings):
                field_mappings.append(match)
            continue
        if column == profile.get("date"):
            continue
        breakdowns.append((column, label))
        seen_breakdown_columns.add(column)
        seen_breakdown_labels.add(label)
        field_mappings.append(match)
    is_monthly = any(word in question for word in ("按月", "每月", "月份", "月度"))
    is_time_series = bool(
        profile["date"]
        and (time_description or is_monthly or any(word in question for word in ("趋势", "走势", "按天", "每日", "每天")))
    )
    if is_time_series:
        if is_monthly:
            x_sql, x_field = (f"DATE_FORMAT({profile['date']}, '%%Y-%%m')" if using_mysql() else f"strftime('%Y-%m', {profile['date']})"), "月份"
        else:
            x_sql, x_field = (f"DATE({profile['date']})" if using_mysql() else f"date({profile['date']})"), "日期"
        series_dimensions = breakdowns
    else:
        if breakdowns:
            x_sql, x_field = breakdowns[0]
            series_dimensions = breakdowns[1:]
        else:
            x_sql = profile["region"] or profile["first_dimension"]
            x_field = "区域" if profile["region"] == x_sql else str(x_sql)
            series_dimensions = []
    if not x_sql:
        raise ValueError("当前数据集没有可用于分组的维度字段")

    used_aliases: set[str] = set()
    x_field = _unique_alias(x_field, used_aliases, "维度")
    series_dimensions = [
        (series_sql, _unique_alias(series_field, used_aliases, "维度"))
        for series_sql, series_field in series_dimensions
    ]
    metric_label = _unique_alias(metric_label, used_aliases, "指标")

    select_parts = [f'{x_sql} AS "{x_field}"']
    group_parts = [x_sql]
    order_parts = [x_sql]
    for series_sql, series_field in series_dimensions:
        select_parts.append(f'{series_sql} AS "{series_field}"')
        group_parts.append(series_sql)
        order_parts.append(series_sql)
    select_parts.append(f'{metric_sql} AS "{metric_label}"')
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    sql = (
        f"SELECT {', '.join(select_parts)} FROM {table_name}{where} "
        f"GROUP BY {', '.join(group_parts)} ORDER BY {', '.join(order_parts)} LIMIT {limit}"
    )
    series_fields = [label for _, label in series_dimensions]
    return QueryPlan(
        sql,
        params,
        x_field,
        metric_label,
        series_fields[0] if series_fields else None,
        series_fields,
        time_description,
        field_mappings,
    )


def _series_label(row: dict[str, Any], series_fields: list[str]) -> str:
    return " / ".join(str(row.get(field) or "未分类") for field in series_fields)


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _jsonable_value(value) for key, value in row.items()}


def _extract_anchors(question: str) -> list[str]:
    """Extract 3 immutable anchor points from the user question.

    A-005 safety mechanism ①: these anchors are the ONLY things the reflector
    is allowed to check — it cannot invent its own quality criteria.

    Anchors are deterministically extracted via keyword matching (no LLM cost).
    Each anchor represents a dimension the answer MUST cover.
    """
    anchors: list[str] = []

    # Priority 1: metric terms — what the user asks to measure
    metric_map = {
        "投诉率": "投诉率分析",
        "投诉": "投诉分析",
        "转化率": "转化率分析",
        "转化": "转化分析",
        "利润": "利润分析",
        "毛利": "利润分析",
        "订单": "订单量分析",
        "销量": "销量分析",
        "销售额": "销售额分析",
        "营收": "销售额分析",
        "金额": "金额分析",
    }
    for keyword, anchor in metric_map.items():
        if keyword in question and anchor not in anchors:
            anchors.append(anchor)
            break

    # Priority 2: dimension terms — what breakdown the user wants
    dim_map = {
        "地区": "地区维度对比",
        "区域": "地区维度对比",
        "大区": "地区维度对比",
        "产品": "产品维度对比",
        "品类": "产品维度对比",
        "类别": "产品维度对比",
        "渠道": "渠道维度对比",
        "达人": "达人维度对比",
        "主播": "达人维度对比",
        "KOL": "达人维度对比",
        "kol": "达人维度对比",
        "KOC": "达人维度对比",
        "koc": "达人维度对比",
        "按月": "时间趋势",
        "每月": "时间趋势",
        "月度": "时间趋势",
        "按天": "时间趋势",
        "每日": "时间趋势",
        "趋势": "时间趋势",
        "走势": "时间趋势",
    }
    for keyword, anchor in dim_map.items():
        if keyword in question and anchor not in anchors:
            anchors.append(anchor)
            if len(anchors) >= 3:
                break

    # Priority 3: analysis depth terms — what kind of insight the user wants
    depth_map = {
        "同比": "同比变化",
        "环比": "环比变化",
        "异常": "异常检测",
        "归因": "原因归因",
        "原因": "原因归因",
        "波动": "波动分析",
        "下滑": "变化趋势",
        "下降": "变化趋势",
        "增长率": "增长率分析",
        "预测": "趋势预测",
        "排行": "排名分析",
        "最高": "排名分析",
        "最低": "排名分析",
    }
    for keyword, anchor in depth_map.items():
        if keyword in question and anchor not in anchors:
            anchors.append(anchor)
            if len(anchors) >= 3:
                break

    # Fallback: if fewer than 2 anchors, add generic ones from the question
    if len(anchors) < 2:
        if "对比" in question or "比较" in question:
            if "对比分析" not in anchors:
                anchors.append("对比分析")
        if "统计" in question or "分析" in question:
            if "数据总结" not in anchors:
                anchors.append("数据总结")
    # Still not enough — fill with generic anchors (no duplicates)
    generic_fallbacks = ["数据总结", "关键发现"]
    for fb in generic_fallbacks:
        if len(anchors) >= 2:
            break
        if fb not in anchors:
            anchors.append(fb)

    return anchors[:3]


def _draft_insights(rows: list[dict[str, Any]], x_field: str, y_field: str, series_fields: list[str]) -> list[str]:
    """Generate deterministic draft insights with statistical depth beyond max/min/total.

    Q-006: Adds mean, std, CV, anomaly detection (>2σ), trend direction, and
    per-category deviation analysis so the draft is richer before LLM polish.
    """
    import math

    if not rows:
        return ["当前筛选条件下没有匹配数据，请调整时间或维度后重试。"]

    if series_fields:
        totals: dict[str, float] = defaultdict(float)
        numeric_rows: list[tuple[dict[str, Any], float]] = []
        for row in rows:
            value = _to_float_or_none(row.get(y_field))
            if value is None:
                continue
            numeric_rows.append((row, value))
            totals[_series_label(row, series_fields)] += value
        if not totals or not numeric_rows:
            return [f"当前结果中「{y_field}」不是可计算的数值指标，建议换用票房、销售额、播放量、金额等数值字段继续分析。"]
        ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        peak, peak_value = max(numeric_rows, key=lambda item: item[1])
        peak_series = _series_label(peak, series_fields)

        # --- 基础排名洞察 ---
        insights = [
            f"{ranked[0][0]}累计{y_field}最高，为 {ranked[0][1]:,.2f}。",
            f"{ranked[-1][0]}累计{y_field}最低，为 {ranked[-1][1]:,.2f}。",
            f"单点峰值出现在 {peak.get(x_field)}，{peak_series}的{y_field}为 {peak_value:,.2f}。",
        ]

        # --- Q-006: 统计深度 ---
        values = list(totals.values())
        n = len(values)
        if n >= 2:
            mean_val = sum(values) / n
            variance = sum((v - mean_val) ** 2 for v in values) / n
            std_val = math.sqrt(variance)
            cv = (std_val / mean_val * 100) if mean_val != 0 else 0

            # 偏离度分析：各品类偏离均值的程度
            deviations = [
                (label, val, (val - mean_val) / mean_val * 100 if mean_val != 0 else 0)
                for label, val in ranked
            ]
            above_avg = [(l, v, d) for l, v, d in deviations if d > 20]
            below_avg = [(l, v, d) for l, v, d in deviations if d < -20]

            if above_avg:
                top_deviant = above_avg[0]
                insights.append(
                    f"「{top_deviant[0]}」显著高于均值 {mean_val:,.2f}（偏离 +{top_deviant[2]:.0f}%），"
                    f"是拉动{y_field}的主要力量。"
                )
            if below_avg:
                bottom_deviant = below_avg[-1]
                insights.append(
                    f"「{bottom_deviant[0]}」显著低于均值（偏离 {bottom_deviant[2]:.0f}%），"
                    f"可能存在优化空间。"
                )

            # 异常检测（>2σ）
            if std_val > 0:
                anomalies = [(l, v) for l, v in ranked if abs(v - mean_val) > 2 * std_val]
                if anomalies:
                    anomaly_desc = "、".join(
                        f"「{l}」({v:,.2f})" for l, v in anomalies[:3]
                    )
                    insights.append(f"⚠ 异常值检测（偏离均值 > 2σ）：{anomaly_desc}。")

            # 集中度
            if n >= 3:
                top3_share = sum(v for _, v in ranked[:3]) / sum(values) * 100
                if top3_share > 80:
                    insights.append(f"集中度偏高：TOP3 品类合计占比 {top3_share:.0f}%，业务依赖集中。")

        return insights

    numeric = []
    for row in rows:
        value = _to_float_or_none(row.get(y_field))
        if value is None:
            continue
        numeric.append((row.get(x_field), value))
    if not numeric:
        return [f"当前结果中「{y_field}」不是可计算的数值指标，建议换用票房、销售额、播放量、金额等数值字段继续分析。"]
    n = len(numeric)
    values_list = [v for _, v in numeric]
    total = sum(values_list)
    mean_val = total / n if n else 0

    highest = max(numeric, key=lambda item: item[1])
    lowest = min(numeric, key=lambda item: item[1])
    insights = [f"{highest[0]}的{y_field}最高，为 {highest[1]:,.2f}。"]

    if n > 1:
        insights.append(f"{lowest[0]}的{y_field}最低，为 {lowest[1]:,.2f}。")

    # --- Q-006: 统计深度（无维度拆分场景）---
    if n >= 2:
        variance = sum((v - mean_val) ** 2 for v in values_list) / n
        std_val = math.sqrt(variance)
        cv = (std_val / mean_val * 100) if mean_val != 0 else 0
        insights.append(
            f"均值 {mean_val:,.2f}，标准差 {std_val:,.2f}（变异系数 {cv:.1f}%），"
            f"合计 {total:,.2f}。"
        )

        # 趋势方向判断（适用于有序 x 轴，如日期）
        if n >= 3:
            first_half = sum(values_list[: n // 2])
            second_half = sum(values_list[n - n // 2 :])
            if second_half > first_half * 1.1:
                direction = "上升"
            elif first_half > second_half * 1.1:
                direction = "下降"
            else:
                direction = "平稳"
            insights.append(f"趋势方向：{direction}（后半段合计 {second_half:,.2f} vs 前半段 {first_half:,.2f}）。")

        # 异常检测（>2σ）
        if std_val > 0:
            anomalies = [
                (label, val)
                for label, val in numeric
                if abs(val - mean_val) > 2 * std_val
            ]
            if anomalies:
                anomaly_desc = "、".join(
                    f"{l}({v:,.2f})" for l, v in anomalies[:3]
                )
                insights.append(f"⚠ 异常值（偏离均值 > 2σ）：{anomaly_desc}。")

        # 极差比
        if lowest[1] > 0:
            range_ratio = highest[1] / lowest[1]
            if range_ratio > 5:
                insights.append(f"极差比 {range_ratio:.1f}:1，数据波动较大，建议关注波动原因。")
    else:
        insights.append(f"当前结果合计为 {total:,.2f}。")

    return insights


TECHNICAL_INSIGHT_RE = re.compile(
    r"(字段别名修正|字段理解|SQL|sql|column_\d+|返回列|实际列|期望|修复|repair|template|query_plan|LLM|字段映射)",
    re.IGNORECASE,
)


def _is_technical_insight(text: Any) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    return bool(TECHNICAL_INSIGHT_RE.search(value))


def _clean_business_insights(items: list[Any] | None, limit: int = 8) -> list[str]:
    cleaned: list[str] = []
    for item in items or []:
        text = str(item or "").strip()
        text = re.sub(r"^[•\-\s]+", "", text)
        if not text or _is_technical_insight(text):
            continue
        if text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _format_metric_value(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 10000:
        return f"{number:,.0f}"
    if number == int(number):
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def _fallback_section_insights(section: dict[str, Any]) -> list[str]:
    rows = section.get("rows") or []
    chart = section.get("chart") or {}
    title = str(section.get("title") or chart.get("title") or "该图")
    x_field = chart.get("x_field") or (section.get("columns") or [""])[0]
    y_field = chart.get("y_field") or ((section.get("columns") or ["", ""])[-1])
    if not rows or not x_field or not y_field:
        return []

    points = []
    for row in rows:
        if row.get(x_field) in (None, ""):
            continue
        try:
            value = float(row.get(y_field) or 0)
        except (TypeError, ValueError):
            continue
        points.append((str(row.get(x_field)), value))
    if not points:
        return []

    insights: list[str] = []
    if chart.get("type") in {"line", "area"} and len(points) >= 2:
        first_label, first_value = points[0]
        last_label, last_value = points[-1]
        direction = "上升" if last_value > first_value else ("回落" if last_value < first_value else "基本持平")
        insights.append(
            f"{title}呈现{direction}态势：从 {first_label} 的 {_format_metric_value(first_value)} "
            f"变化到 {last_label} 的 {_format_metric_value(last_value)}，建议结合活动、季节或渠道变化解释波动原因。"
        )

    ranked = sorted(points, key=lambda item: item[1], reverse=True)
    top_label, top_value = ranked[0]
    bottom_label, bottom_value = ranked[-1]
    if len(ranked) == 1:
        insights.append(f"{title}当前只有一个可比较对象，{top_label} 的 {y_field} 为 {_format_metric_value(top_value)}，建议补充更多分组后再判断结构差异。")
    else:
        insights.append(
            f"{top_label}在「{title}」中表现最突出，{y_field}达到 {_format_metric_value(top_value)}，"
            "说明它对当前结果的拉动最明显，值得优先拆解其来源。"
        )
        if bottom_value != top_value:
            insights.append(
                f"{bottom_label}相对偏低，{y_field}为 {_format_metric_value(bottom_value)}；"
                "这种头尾差距说明不同对象之间表现不均衡，后续应继续比较类型、时间或渠道差异。"
            )
    return insights[:3]


def _fallback_overall_insights(
    *,
    question: str,
    dataset: dict,
    chart_sections: list[dict[str, Any]],
    base_insights: list[str],
    omitted_chart_sections: list[str],
) -> list[str]:
    insights = _clean_business_insights(base_insights, limit=4)
    if not insights and chart_sections:
        first_section = chart_sections[0]
        insights.extend(_fallback_section_insights(first_section)[:2])
    if chart_sections:
        section_names = "、".join(str(section.get("title") or "") for section in chart_sections[:4] if section.get("title"))
        insights.insert(
            0,
            f"这次分析已经围绕「{dataset.get('name', '当前数据源')}」拆出了 {len(chart_sections)} 个观察角度"
            + (f"（{section_names}）" if section_names else "")
            + "，更适合先看结构差异，再继续下钻原因。"
        )
    if omitted_chart_sections:
        insights.append(
            f"由于页面最多展示 {MAX_CHART_SECTIONS} 张图，本次没有展开「{'、'.join(omitted_chart_sections[:5])}」；"
            "如果你想看这些维度，可以直接追问其中一个。"
        )
    insights.append("后续可以继续追问：按时间拆解变化原因、对高值对象做来源归因，或指定某个维度单独生成图表。")
    result: list[str] = []
    for item in insights:
        if item and item not in result and not _is_technical_insight(item):
            result.append(item)
    return result[:6]


async def _apply_database_business_narrative(
    *,
    question: str,
    dataset: dict,
    rows: list[dict[str, Any]],
    insights: list[str],
    knowledge: list[dict[str, Any]],
    chart_sections: list[dict[str, Any]],
    omitted_chart_sections: list[str],
    technical_notes: list[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    """Hide engineering details and rewrite database output as business advice."""
    clean_draft = _clean_business_insights(insights, limit=8)
    updated_sections: list[dict[str, Any]] = []
    for section in chart_sections:
        section_copy = dict(section)
        section_copy["insights"] = _clean_business_insights(section.get("insights") or [], limit=3)
        updated_sections.append(section_copy)

    narrative = await narrate_database_insights(
        question=question,
        dataset_name=str(dataset.get("name") or ""),
        rows=rows,
        draft=clean_draft,
        chart_sections=updated_sections,
        knowledge=knowledge,
        omitted_chart_sections=omitted_chart_sections,
        technical_notes=technical_notes or [],
    )

    section_map = {
        str(item.get("id")): _clean_business_insights(item.get("insights") or [], limit=3)
        for item in (narrative.get("sections") or [])
        if isinstance(item, dict)
    }
    narrative_applied = bool(narrative.get("overall") or section_map)

    for section in updated_sections:
        section_id = str(section.get("id") or "")
        generated = section_map.get(section_id) or []
        if generated:
            section["insights"] = generated
        elif not section.get("insights"):
            section["insights"] = _fallback_section_insights(section)

    overall = _clean_business_insights(narrative.get("overall") or [], limit=4)
    if not overall:
        overall = _fallback_overall_insights(
            question=question,
            dataset=dataset,
            chart_sections=updated_sections,
            base_insights=clean_draft,
            omitted_chart_sections=omitted_chart_sections,
        )
    elif omitted_chart_sections:
        overall.append(
            f"受图表数量限制，本次未展开「{'、'.join(omitted_chart_sections[:5])}」；需要的话可以继续指定其中一个维度。"
        )

    followups = _clean_business_insights(narrative.get("followups") or [], limit=4)
    if followups:
        overall.append("你可以继续追问：" + "；".join(followups[:3]) + "。")

    final_overall: list[str] = []
    for item in overall:
        if item and item not in final_overall and not _is_technical_insight(item):
            final_overall.append(item)
    return final_overall[:7], updated_sections, narrative_applied


def _needs_python_analysis(question: str, intent_label: str | None) -> bool:
    python_terms = (
        "同比",
        "环比",
        "异常",
        "归因",
        "原因",
        "波动",
        "下滑",
        "下降",
        "增长率",
        "变化率",
        "趋势",
        "走势",
        "预测",
    )
    if any(term in question for term in python_terms):
        return True
    return intent_label in {"trend_analysis", "anomaly_attribution"}


def _python_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value[:50]:
        if isinstance(item, dict):
            rows.append({str(key): _jsonable_value(val) for key, val in item.items()})
    return rows


def _apply_python_chart(chart: dict[str, Any], suggestion: Any) -> dict[str, Any]:
    if not isinstance(suggestion, dict):
        return chart
    allowed_types = {"bar", "line", "pie", "scatter", "area", "radar", "none"}
    chart_type = str(suggestion.get("type") or chart["type"]).lower()
    if chart_type not in allowed_types:
        chart_type = chart["type"]
    updated = dict(chart)
    updated["type"] = chart_type
    updated["x_field"] = suggestion.get("x") or suggestion.get("x_field") or chart.get("x_field")
    updated["y_field"] = suggestion.get("y") or suggestion.get("y_field") or chart.get("y_field")
    updated["series_field"] = suggestion.get("series") or suggestion.get("series_field") or chart.get("series_field")
    updated["series_name"] = updated["y_field"]
    updated["recommendation"] = {
        "type": chart_type,
        "source": "python_suggestion",
        "reason": suggestion.get("reason") or "Python/Pandas 分析脚本返回了 chart_suggestion",
        "confidence": suggestion.get("confidence") or 0.86,
        "alternatives": chart.get("alternatives", []),
        "display_mode": chart.get("display_mode", "single"),
        "secondary_y_field": chart.get("secondary_y_field"),
        "facet_fields": chart.get("facet_fields", []),
        "distribution": (chart.get("recommendation") or {}).get("distribution", {}),
    }
    return updated


def _wants_multi_chart(question: str) -> bool:
    return any(term in question for term in ("完整分析", "全面分析", "多维度", "多角度", "综合分析", "分析报告", "完整报告", "各维度", "每一组"))


def _chart_section(
    *,
    section_id: str,
    title: str,
    description: str,
    rows: list[dict[str, Any]],
    chart: dict[str, Any],
    insights: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": section_id,
        "title": title,
        "description": description,
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
        "chart": chart,
        "insights": insights or [],
    }


def _chart_from_plan(
    *,
    question: str,
    intent_label: str,
    dataset_name: str,
    rows: list[dict[str, Any]],
    query_plan: QueryPlan,
    title_prefix: str = "",
) -> dict[str, Any]:
    recommendation = recommend_chart(
        question,
        intent_label,
        rows,
        query_plan.x_field,
        query_plan.y_field,
        query_plan.series_fields,
    )
    chart_type = recommendation["type"]
    title_bits = [dataset_name]
    if title_prefix:
        title_bits.append(title_prefix)
    if query_plan.time_description:
        title_bits.append(query_plan.time_description)
    title_bits.append(query_plan.y_field)
    return {
        "type": chart_type,
        "title": " - ".join(str(item) for item in title_bits if item),
        "x_field": query_plan.x_field,
        "y_field": query_plan.y_field,
        "series_name": query_plan.y_field,
        "series_field": query_plan.series_field,
        "series_fields": query_plan.series_fields,
        "recommendation": recommendation,
        "alternatives": recommendation.get("alternatives", []),
        "display_mode": recommendation.get("display_mode", "single"),
        "secondary_y_field": recommendation.get("secondary_y_field"),
        "facet_fields": recommendation.get("facet_fields", []),
    }


def _run_template_query(dataset: dict, query: str, limit: int) -> tuple[QueryPlan, list[dict[str, Any]]] | None:
    try:
        query_plan = _build_query(query, dataset, min(limit, 200))
        with connect() as conn:
            result = conn.execute(query_plan.sql, query_plan.params).fetchall()
        rows = [_jsonable_row(dict(row)) for row in result]
        if not rows:
            return None
        if query_plan.x_field not in rows[0] or query_plan.y_field not in rows[0]:
            return None
        return query_plan, rows
    except Exception:
        return None


def _run_group_query(
    dataset: dict,
    *,
    dimension_column: str | None,
    dimension_label: str,
    metric_column: str | None,
    metric_label: str,
    limit: int,
    descending: bool = True,
) -> tuple[QueryPlan, list[dict[str, Any]]] | None:
    if not dimension_column or not metric_column:
        return None
    row_limit = max(1, min(int(limit or 20), 200))
    table_name = dataset["table_name"]
    order_direction = "DESC" if descending else "ASC"
    sql = (
        f'SELECT {dimension_column} AS "{dimension_label}", '
        f'ROUND(SUM({metric_column}), 2) AS "{metric_label}" '
        f"FROM {table_name} "
        f"WHERE {dimension_column} IS NOT NULL AND {dimension_column} != '' "
        f"GROUP BY {dimension_column} ORDER BY 2 {order_direction} LIMIT {row_limit}"
    )
    try:
        safe_sql = validate_readonly_sql(sql, table_name)
        with connect() as conn:
            result = conn.execute(safe_sql).fetchall()
        rows = [_jsonable_row(dict(row)) for row in result]
        if not rows:
            return None
        plan = QueryPlan(
            sql=safe_sql,
            params=[],
            x_field=dimension_label,
            y_field=metric_label,
            series_field=None,
            series_fields=[],
            time_description=None,
        )
        return plan, rows
    except Exception:
        return None


def _with_chart_type(chart: dict[str, Any], chart_type: str, reason: str | None = None) -> dict[str, Any]:
    updated = dict(chart)
    updated["type"] = chart_type
    recommendation = dict(updated.get("recommendation") or {})
    recommendation["type"] = chart_type
    if reason:
        recommendation["reason"] = reason
    updated["recommendation"] = recommendation
    return updated


def _to_float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _floatable(value: Any) -> bool:
    return _to_float_or_none(value) is not None


def _infer_chart_fields(rows: list[dict[str, Any]]) -> tuple[str, str]:
    if not rows:
        return "", ""
    columns = list(rows[0].keys())
    numeric_columns = [column for column in columns if any(_floatable(row.get(column)) for row in rows[:20])]
    y_field = numeric_columns[-1] if numeric_columns else (columns[-1] if columns else "")
    x_field = next((column for column in columns if column != y_field and not any(_floatable(row.get(column)) for row in rows[:20])), "")
    if not x_field:
        x_field = next((column for column in columns if column != y_field), columns[0] if columns else "")
    return x_field, y_field


def _column_is_numeric(rows: list[dict[str, Any]], column: str) -> bool:
    values = [row.get(column) for row in rows[:30]]
    useful = [value for value in values if value not in (None, "")]
    if not useful:
        return False
    numeric_count = sum(1 for value in useful if _to_float_or_none(value) is not None)
    return numeric_count >= max(1, len(useful) // 2)


def _expected_alias_group(expected: str) -> str | None:
    normalized_expected = normalize_alias_text(expected)
    if not normalized_expected:
        return None
    preferred = {
        "区域": "地区",
        "地区": "地区",
        "产品类别": "产品",
        "产品收入结构": "产品",
        "渠道结构": "渠道",
        "客户集中度": "客户",
        "达人贡献": "达人",
        "回款风险近似": "应收账款",
    }
    if expected in preferred:
        return preferred[expected]
    for group, aliases in FIELD_ALIAS_GROUPS.items():
        candidates = (group, *aliases)
        for alias in candidates:
            normalized_alias = normalize_alias_text(alias)
            if normalized_alias and (normalized_alias in normalized_expected or normalized_expected in normalized_alias):
                return group
    return None


def _result_column_score(
    *,
    expected: str,
    actual: str,
    rows: list[dict[str, Any]],
    dataset: dict,
    prefer_numeric: bool | None,
) -> float:
    expected_norm = normalize_alias_text(expected)
    actual_norm = normalize_alias_text(actual)
    if not actual_norm:
        return -100.0

    score = 0.0
    if expected_norm == actual_norm:
        score += 80
    elif expected_norm and (expected_norm in actual_norm or actual_norm in expected_norm):
        score += 35

    group = _expected_alias_group(expected)
    if group:
        for alias in FIELD_ALIAS_GROUPS.get(group, ()):
            alias_norm = normalize_alias_text(alias)
            if alias_norm and alias_norm in actual_norm:
                score += 28
                break

    metadata = next((column for column in dataset.get("columns", []) if column.get("name") == actual), None)
    if metadata and group:
        metadata_text = normalize_alias_text(f"{metadata.get('name', '')} {metadata.get('description', '')}")
        for alias in FIELD_ALIAS_GROUPS.get(group, ()):
            alias_norm = normalize_alias_text(alias)
            if alias_norm and alias_norm in metadata_text:
                score += 34
                break

    is_numeric = _column_is_numeric(rows, actual)
    if prefer_numeric is True:
        score += 18 if is_numeric else -35
        if metadata and _is_index_like_column_meta(metadata):
            score -= 45
        if any(token in actual_norm for token in ("gmv", "sales", "revenue", "amount", "paid", "total", "sum", "销售", "营收", "收入", "金额")):
            score += 10
    elif prefer_numeric is False:
        score += 15 if not is_numeric else -8
        if any(token in actual_norm for token in ("id", "name", "type", "category", "brand", "region", "channel", "creator", "customer", "product")):
            score += 8

    return score


def _best_result_column(
    *,
    expected: str,
    rows: list[dict[str, Any]],
    dataset: dict,
    prefer_numeric: bool | None,
    exclude: set[str] | None = None,
) -> tuple[str | None, float]:
    if not rows:
        return None, -100.0
    exclude = exclude or set()
    candidates = [column for column in rows[0].keys() if column not in exclude]
    if expected in candidates:
        if prefer_numeric is True and not _column_is_numeric(rows, expected):
            candidates = [column for column in candidates if column != expected]
        elif prefer_numeric is False and _column_is_numeric(rows, expected):
            candidates = [column for column in candidates if column != expected]
        else:
            return expected, 100.0
    scored = [
        (
            column,
            _result_column_score(
                expected=expected,
                actual=column,
                rows=rows,
                dataset=dataset,
                prefer_numeric=prefer_numeric,
            ),
        )
        for column in candidates
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    if scored and scored[0][1] >= 12:
        return scored[0]

    # 兜底：LLM SQL 已经返回了有效数据时，按类型选一个最像维度/指标的列，
    # 不因为别名不同就把整个分析判失败。
    if prefer_numeric is True:
        metadata_by_name = {str(column.get("name")): column for column in dataset.get("columns", [])}
        numeric = [
            column
            for column in candidates
            if _column_is_numeric(rows, column)
            and not _is_index_like_column_meta(metadata_by_name.get(str(column), {}))
        ]
        return (numeric[-1], 1.0) if numeric else (None, -100.0)
    if prefer_numeric is False:
        dimensions = [column for column in candidates if not _column_is_numeric(rows, column)]
        return (dimensions[0], 1.0) if dimensions else (None, -100.0)
    return (candidates[0], 1.0) if candidates else (None, -100.0)


def _normalize_result_columns(
    rows: list[dict[str, Any]],
    query_plan: QueryPlan,
    dataset: dict,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not rows:
        return rows, {}

    mapping: dict[str, str] = {}
    used: set[str] = set()

    if query_plan.x_field and query_plan.x_field not in rows[0]:
        actual, _ = _best_result_column(
            expected=query_plan.x_field,
            rows=rows,
            dataset=dataset,
            prefer_numeric=False,
        )
        if actual:
            mapping[query_plan.x_field] = actual
            used.add(actual)

    if query_plan.y_field and query_plan.y_field not in rows[0]:
        actual, _ = _best_result_column(
            expected=query_plan.y_field,
            rows=rows,
            dataset=dataset,
            prefer_numeric=True,
            exclude=used,
        )
        if actual:
            mapping[query_plan.y_field] = actual
            used.add(actual)

    for expected in query_plan.series_fields:
        if expected and expected not in rows[0]:
            actual, _ = _best_result_column(
                expected=expected,
                rows=rows,
                dataset=dataset,
                prefer_numeric=False,
                exclude=used,
            )
            if actual:
                mapping[expected] = actual
                used.add(actual)

    if mapping:
        for row in rows:
            for expected, actual in mapping.items():
                if expected not in row and actual in row:
                    row[expected] = row.get(actual)
    return rows, mapping


def _coerce_query_plan_numeric_fields(
    rows: list[dict[str, Any]],
    query_plan: QueryPlan,
    dataset: dict,
) -> tuple[QueryPlan, list[dict[str, str]]]:
    """Keep chart/insight fields type-safe after LLM SQL or alias repair.

    LLM SQL sometimes returns a semantically close column under the expected alias,
    but the value type can still be wrong (for example movie title text being used
    as the metric).  This guard picks a real numeric metric and a real dimension
    from the returned rows instead of letting float() crash later.
    """
    if not rows:
        return query_plan, []

    first = rows[0]
    columns = list(first.keys())
    corrections: list[dict[str, str]] = []
    y_field = query_plan.y_field
    x_field = query_plan.x_field

    if not y_field or y_field not in first or not _column_is_numeric(rows, y_field):
        actual_y, _ = _best_result_column(
            expected=y_field or "指标",
            rows=rows,
            dataset=dataset,
            prefer_numeric=True,
        )
        if actual_y and actual_y in first and _column_is_numeric(rows, actual_y):
            if actual_y != y_field:
                corrections.append({"field": "metric", "from": str(y_field or ""), "to": str(actual_y)})
            y_field = actual_y

    if not x_field or x_field not in first or x_field == y_field or _column_is_numeric(rows, x_field):
        actual_x, _ = _best_result_column(
            expected=x_field or "维度",
            rows=rows,
            dataset=dataset,
            prefer_numeric=False,
            exclude={y_field} if y_field else set(),
        )
        if actual_x and actual_x in first and actual_x != y_field:
            if actual_x != x_field:
                corrections.append({"field": "dimension", "from": str(x_field or ""), "to": str(actual_x)})
            x_field = actual_x

    if (not x_field or x_field == y_field) and columns:
        fallback_x = next((column for column in columns if column != y_field), columns[0])
        if fallback_x != x_field:
            corrections.append({"field": "dimension", "from": str(x_field or ""), "to": str(fallback_x)})
        x_field = fallback_x

    series_fields = [
        field
        for field in query_plan.series_fields
        if field in first and field not in {x_field, y_field} and not _column_is_numeric(rows, field)
    ]
    if series_fields != query_plan.series_fields:
        corrections.append(
            {
                "field": "series",
                "from": "、".join(query_plan.series_fields),
                "to": "、".join(series_fields),
            }
        )

    updated = replace(
        query_plan,
        x_field=x_field,
        y_field=y_field,
        series_fields=series_fields,
        series_field=series_fields[0] if series_fields else None,
    )
    return updated, corrections


def _build_data_chart_sections(
    *,
    question: str,
    intent_label: str,
    dataset: dict,
    rows: list[dict[str, Any]],
    chart: dict[str, Any],
    query_plan: QueryPlan,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not rows or chart.get("type") == "none":
        return [], []

    sections: list[dict[str, Any]] = []
    omitted: list[str] = []
    seen: set[tuple[str, str, tuple[str, ...], str]] = set()
    multi_chart_requested = _wants_multi_chart(question)

    def add_section(
        section_id: str,
        title: str,
        description: str,
        section_rows: list[dict[str, Any]],
        section_chart: dict[str, Any],
        section_plan: QueryPlan,
        insights: list[str] | None = None,
        discriminator: str = "",
    ) -> None:
        if not section_rows or section_chart.get("type") == "none":
            return
        key = (section_plan.x_field, section_plan.y_field, tuple(section_plan.series_fields), discriminator)
        if key in seen:
            return
        seen.add(key)
        if len(sections) >= MAX_CHART_SECTIONS:
            if title not in omitted:
                omitted.append(title)
            return
        sections.append(
            _chart_section(
                section_id=section_id,
                title=title,
                description=description,
                rows=section_rows[:200],
                chart=section_chart,
                insights=insights,
            )
        )

    if not multi_chart_requested and query_plan.x_field in rows[0] and query_plan.y_field in rows[0]:
        add_section(
            "main",
            chart.get("title") or "核心图表",
            "当前问题对应的主分析结果。",
            rows,
            chart,
            query_plan,
        )

    if query_plan.series_fields and not multi_chart_requested:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[_series_label(row, query_plan.series_fields)].append(row)
        grouped_items = list(grouped.items())
        for index, (series_name, group_rows) in enumerate(grouped_items[: MAX_CHART_SECTIONS + 8], 1):
            section_plan = QueryPlan(
                query_plan.sql,
                query_plan.params,
                query_plan.x_field,
                query_plan.y_field,
                None,
                [],
                query_plan.time_description,
            )
            recommendation = recommend_chart(question, intent_label, group_rows, query_plan.x_field, query_plan.y_field, [])
            section_chart = dict(chart)
            section_chart.update(
                {
                    "type": recommendation.get("type") or chart.get("type"),
                    "title": f"{series_name} - {query_plan.y_field}",
                    "series_field": None,
                    "series_fields": [],
                    "recommendation": recommendation,
                    "alternatives": recommendation.get("alternatives", []),
                    "display_mode": recommendation.get("display_mode", "single"),
                    "secondary_y_field": recommendation.get("secondary_y_field"),
                    "facet_fields": recommendation.get("facet_fields", []),
                }
            )
            add_section(
                f"series-{index}",
                series_name,
                f"从「{series_name}」这一组单独观察 {query_plan.y_field}。",
                group_rows,
                section_chart,
                section_plan,
                discriminator=series_name,
            )
        if len(grouped_items) > MAX_CHART_SECTIONS + 8:
            omitted.append(f"其余 {len(grouped_items) - MAX_CHART_SECTIONS - 8} 个细分分组")

    if multi_chart_requested:
        profile = _column_profile(dataset["columns"])
        primary_metric, primary_metric_label = _primary_metric(dataset, profile)

        def add_group_dimension(
            key: str,
            title: str,
            description: str,
            dimension_column: str | None,
            metric_column: str | None,
            metric_label: str,
            preferred_chart: str | None = None,
            row_limit: int | None = None,
        ) -> None:
            result = _run_group_query(
                dataset,
                dimension_column=dimension_column,
                dimension_label=title,
                metric_column=metric_column,
                metric_label=metric_label,
                limit=row_limit or limit,
            )
            if not result:
                return
            section_plan, section_rows = result
            section_chart = _chart_from_plan(
                question=f"{question} {title}",
                intent_label=intent_label,
                dataset_name=dataset["name"],
                rows=section_rows,
                query_plan=section_plan,
                title_prefix=title,
            )
            if preferred_chart:
                section_chart = _with_chart_type(
                    section_chart,
                    preferred_chart,
                    f"{title}更适合用{preferred_chart}观察结构占比或排序差异。",
                )
            add_section(
                f"dimension-{key}",
                title,
                description,
                section_rows,
                section_chart,
                section_plan,
            )

        def add_template_dimension(
            key: str,
            title: str,
            derived_question: str,
            description: str,
            preferred_chart: str | None = None,
        ) -> None:
            result = _run_template_query(dataset, derived_question, limit)
            if not result:
                return
            section_plan, section_rows = result
            section_chart = _chart_from_plan(
                question=derived_question,
                intent_label=intent_label,
                dataset_name=dataset["name"],
                rows=section_rows,
                query_plan=section_plan,
                title_prefix=title,
            )
            if preferred_chart:
                section_chart = _with_chart_type(
                    section_chart,
                    preferred_chart,
                    f"{title}更适合用{preferred_chart}查看趋势或变化。",
                )
            add_section(
                f"dimension-{key}",
                title,
                description,
                section_rows,
                section_chart,
                section_plan,
            )

        # 优先补全更像“经营分析报告”的维度，而不是只按地区/产品机械拆分。
        if profile.get("date"):
            add_template_dimension(
                "time",
                "时间趋势",
                f"{question} 按月趋势分析",
                "用于判断指标随时间的增长、回落或波动。",
                "line",
            )
        if profile.get("customer"):
            add_group_dimension(
                "customer-concentration",
                "客户集中度",
                "按客户贡献度观察是否存在大客户依赖或收入集中风险。",
                profile.get("customer"),
                primary_metric,
                primary_metric_label,
                "pie",
                row_limit=10,
            )
        if profile.get("aging") and (profile.get("receivables") or primary_metric):
            add_group_dimension(
                "receivable-aging",
                "应收账款账龄",
                "按账龄/逾期维度观察回款风险；如果表内没有应收字段，则先使用当前最接近的金额指标近似展示。",
                profile.get("aging"),
                profile.get("receivables") or primary_metric,
                "应收账款" if profile.get("receivables") else primary_metric_label,
                "bar",
            )
        elif any(term in question for term in ("应收账款", "应收", "账龄", "回款", "逾期")) and profile.get("status"):
            add_group_dimension(
                "receivable-risk-proxy",
                "回款风险近似",
                "数据表未发现明确的应收账款/账龄字段，先用结算状态、订单状态或退款金额等最接近字段做近似风险观察。",
                profile.get("status"),
                profile.get("refund") or primary_metric,
                "退款金额" if profile.get("refund") else primary_metric_label,
                "bar",
            )
        if profile.get("cash_flow") and profile.get("date"):
            add_template_dimension(
                "cash-flow",
                "现金流状况",
                f"{question} 按月分析现金流",
                "用于观察现金流入流出或净现金流变化。",
                "line",
            )
        if profile.get("profit") and profile.get("date"):
            add_template_dimension(
                "profit",
                "盈利能力",
                f"{question} 按月分析利润",
                "用于观察利润走势与收入规模是否同步。",
                "area",
            )
        if profile.get("product"):
            add_group_dimension(
                "product",
                "产品收入结构",
                "按产品/品类拆分，观察主要收入来源与结构差异。",
                profile.get("product"),
                primary_metric,
                primary_metric_label,
                "pie",
                row_limit=12,
            )
        if profile.get("channel"):
            add_group_dimension(
                "channel",
                "渠道结构",
                "按渠道拆分，观察不同来源的贡献度。",
                profile.get("channel"),
                primary_metric,
                primary_metric_label,
                "bar",
            )
        if profile.get("region"):
            add_group_dimension(
                "region",
                "区域维度",
                "按地区拆分，观察区域经营差异。",
                profile.get("region"),
                primary_metric,
                primary_metric_label,
                "bar",
            )
        if profile.get("influencer"):
            add_group_dimension(
                "influencer",
                "达人贡献",
                "按达人/主播/创作者拆分，观察带货贡献与头部依赖。",
                profile.get("influencer"),
                primary_metric,
                primary_metric_label,
                "bar",
                row_limit=12,
            )

    return (sections if len(sections) > 1 else []), omitted


def _sql_repair_stats(attempts: list[dict[str, Any]], success: bool) -> dict[str, Any]:
    repair_attempts = max(0, len(attempts) - 1)
    return {
        "attempts": len(attempts),
        "repair_attempts": repair_attempts,
        "repaired": success and repair_attempts > 0,
        "success": success,
        "repair_success_rate": (1.0 if success else 0.0) if repair_attempts else None,
        "history": attempts,
    }


async def _execute_sql_with_repair(
    question: str,
    dataset: dict,
    query_plan: QueryPlan,
    limit: int,
    business_knowledge: list[dict] | None = None,
    intent_reason: str = "",
) -> tuple[str, list[dict[str, Any]], str, dict[str, Any]]:
    settings = get_settings()
    template_sql = query_plan.sql
    attempts: list[dict[str, Any]] = []
    field_not_found_fallback: str | None = None
    try:
        llm_sql = await generate_llm_sql(
            question, dataset, limit,
            business_knowledge=business_knowledge,
            intent_reason=intent_reason,
        )
        plan_source = query_plan_source(question, llm_sql)
    except FieldNotFoundError as exc:
        # 不把字段识别失败直接暴露成“分析失败”。先回退到模板 SQL/相近字段，
        # 再在最终洞察里说明采用了近似口径，方便用户后续修正字段。
        llm_sql = None
        plan_source = "template_sql_field_fallback"
        field_not_found_fallback = exc.message
        attempts.append(
            {
                "attempt": 0,
                "success": False,
                "source": "llm_sql",
                "sql": "",
                "error": f"字段识别失败，已回退到可用字段：{exc.message}",
            }
        )
    candidate_sql = llm_sql or template_sql
    candidate_params: list[Any] = [] if llm_sql else query_plan.params

    for attempt_no in range(1, 4):
        try:
            safe_sql = validate_readonly_sql(candidate_sql, dataset["table_name"])
            with connect() as conn:
                result = conn.execute(safe_sql, candidate_params).fetchall()
                rows = [_jsonable_row(dict(row)) for row in result]
            rows, column_alias_mapping = _normalize_result_columns(rows, query_plan, dataset)
            if rows and query_plan.y_field and query_plan.y_field not in rows[0]:
                raise ValueError(
                    f"LLM 生成的 SQL 缺少期望的指标列 \"{query_plan.y_field}\"，"
                    f"实际列: {list(rows[0].keys())[:8]}"
                )
            if rows and query_plan.x_field and query_plan.x_field not in rows[0]:
                raise ValueError(
                    f"LLM 生成的 SQL 缺少期望的维度列 \"{query_plan.x_field}\"，"
                    f"实际列: {list(rows[0].keys())[:8]}"
                )
            attempts.append(
                {
                    "attempt": attempt_no,
                    "success": True,
                    "source": plan_source,
                    "sql": safe_sql,
                    "error": None,
                }
            )
            stats = _sql_repair_stats(attempts, True)
            if field_not_found_fallback:
                stats["field_not_found_fallback"] = field_not_found_fallback
            if column_alias_mapping:
                stats["column_alias_mapping"] = column_alias_mapping
            return safe_sql, rows, plan_source, stats
        except Exception as exc:
            error = str(exc)
            attempts.append(
                {
                    "attempt": attempt_no,
                    "success": False,
                    "source": plan_source,
                    "sql": candidate_sql,
                    "error": error,
                }
            )
            if attempt_no >= 3:
                break
            if candidate_sql != template_sql and "缺少期望" in error:
                candidate_sql = template_sql
                candidate_params = query_plan.params
                plan_source = "template_sql_column_fallback"
                continue
            repaired_sql = None
            if settings.llm_configured:
                repaired_sql = await repair_llm_sql(
                    question, dataset, limit, candidate_sql, error,
                    business_knowledge=business_knowledge,
                    intent_reason=intent_reason,
                )
            if repaired_sql and repaired_sql != candidate_sql:
                candidate_sql = repaired_sql
                candidate_params = []
                plan_source = "llm_sql_repair"
                continue
            if candidate_sql != template_sql:
                candidate_sql = template_sql
                candidate_params = query_plan.params
                plan_source = "template_sql_repair_fallback"
                continue
            break

    repair_stats = _sql_repair_stats(attempts, False)
    if field_not_found_fallback:
        repair_stats["field_not_found_fallback"] = field_not_found_fallback
    last_error = attempts[-1]["error"] if attempts else "未知 SQL 执行错误"
    raise ValueError(f"SQL 自动修复失败：{last_error}; repair_stats={json.dumps(repair_stats, ensure_ascii=False)}")


def _load_history(session_id: str | None) -> list[dict[str, Any]]:
    if not session_id:
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content, payload, created_at FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        if item.get("payload"):
            try:
                item["payload"] = json.loads(item["payload"])
            except json.JSONDecodeError:
                item["payload"] = None
        history.append(item)
    return history


def _last_effective_question(history: list[dict[str, Any]]) -> str | None:
    for item in reversed(history):
        payload = item.get("payload")
        if item.get("role") == "assistant" and isinstance(payload, dict) and payload.get("effective_question"):
            return str(payload["effective_question"])
    for item in reversed(history):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return None


def _previous_answer_type(history: list[dict[str, Any]]) -> str | None:
    for item in reversed(history):
        payload = item.get("payload")
        if item.get("role") == "assistant" and isinstance(payload, dict):
            return payload.get("answer_type") or ("knowledge_qa" if payload.get("intent") == "知识库问答" else "data_analysis")
    return None


def _looks_like_followup(question: str, history: list[dict[str, Any]]) -> bool:
    if not history:
        return False
    markers = ("只看", "改成", "改为", "换成", "再看", "继续", "其中", "那么", "这个", "该指标", "按", "解释", "为什么", "呢")
    return any(marker in question for marker in markers) or len(question.strip()) <= 12


def _is_knowledge_question(question: str, history: list[dict[str, Any]]) -> bool:
    knowledge_signals = ("什么是", "如何计算", "怎么计算", "怎样计算", "口径", "定义", "含义", "业务规则", "制度", "知识库", "字段说明")
    data_signals = ("统计", "分析", "趋势", "最高", "最低", "排行", "同比", "环比", "图表", "多少")
    if any(signal in question for signal in knowledge_signals) and not any(signal in question for signal in data_signals):
        return True
    return _previous_answer_type(history) == "knowledge_qa" and _looks_like_followup(question, history)


def _merge_followup(question: str, history: list[dict[str, Any]]) -> tuple[str, bool]:
    base = _last_effective_question(history)
    if not base or not _looks_like_followup(question, history):
        return question, False

    current = question.strip()
    merged_base = base
    if re.search(r"(?:近|最近)\s*\d+\s*(?:天|日|周|个月|月)|本月|今年|本年", current):
        merged_base = re.sub(r"(?:近|最近)\s*\d+\s*(?:天|日|周|个月|月)|本月|今年|本年", "", merged_base)
    if any(term in current for term in ("销售额", "利润", "订单", "销量", "投诉率", "投诉", "转化率", "转化")):
        merged_base = re.sub(r"销售额|毛利润|利润|订单数|订单|销量|投诉率|投诉|转化率|转化", "", merged_base)
    dimension_replace = any(term in current for term in (
        "只按", "仅按", "不看地区", "不看区域", "去掉地区", "去掉区域", "不要地区", "不要区域",
    ))
    if dimension_replace:
        merged_base = re.sub(r"(?:各|按)?(?:地区|区域|大区|产品类别|产品|展品|品类|类别|渠道|达人|主播|KOL|KOC|kol|koc|博主|创作者)(?:拆分|分组|展示|对比)?", "", merged_base)
    if any(region in current for region in REGIONS):
        for region in REGIONS:
            merged_base = merged_base.replace(region, "")
    merged_base = re.sub(r"\s+", " ", merged_base).strip(" ，。；")
    return f"{merged_base}；{current}" if merged_base else current, True


def _actor_id_from_history(history: list[dict]) -> int | None:
    """Extract user/actor ID from session history if available."""
    with connect() as conn:
        for item in reversed(history):
            payload = item.get("payload")
            if isinstance(payload, dict) and payload.get("session_id"):
                row = conn.execute("SELECT user_id FROM sessions WHERE id = ?", (payload["session_id"],)).fetchone()
                if row and row.get("user_id"):
                    return row["user_id"]
    return None


def _store_user(session_id: str, question: str, dataset_id: int | None) -> None:
    with connect() as conn:
        session = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            conn.execute(
                "INSERT INTO sessions(id, title, dataset_id) VALUES (?, ?, ?)",
                (session_id, question[:40], dataset_id),
            )
        elif dataset_id is not None:
            conn.execute("UPDATE sessions SET dataset_id = ? WHERE id = ?", (dataset_id, session_id))
        conn.execute("INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)", (session_id, question))
        conn.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))


def _store_assistant(session_id: str, payload: dict, audit_action: str, dataset_id: int | None) -> None:
    content = "\n".join(payload.get("insights") or [payload.get("message", "")])
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages(session_id, role, content, payload) VALUES (?, 'assistant', ?, ?)",
            (session_id, content, json.dumps(payload, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO audit_logs(action, resource_type, resource_id, detail) VALUES (?, 'session', ?, ?)",
            (audit_action, session_id, payload.get("effective_question", "")),
        )
        conn.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))


def _business_feedback_payload(
    *,
    session_id: str,
    question: str,
    effective_question: str,
    message: str,
    intent_result: IntentResult,
    dataset: dict | None = None,
    sql: str = "",
    columns: list[str] | None = None,
    knowledge: list[dict[str, Any]] | None = None,
    context_applied: bool = False,
) -> dict[str, Any]:
    """Return a normal assistant answer for user/data issues instead of a system error."""
    plan_steps = build_plan_steps(
        intent=intent_result,
        answer_type="data_analysis",
        execution_mode="business-feedback",
        dataset_name=(dataset or {}).get("name", ""),
        chart_type="none",
    )
    return {
        "session_id": session_id,
        "message": message,
        "intent": intent_result.display_name,
        "intent_label": intent_result.label,
        "intent_confidence": intent_result.confidence,
        "intent_method": intent_result.method,
        "intent_reason": intent_result.reason,
        "sql_source": "business_feedback",
        "plan": plan_titles(plan_steps),
        "plan_steps": plan_steps,
        "sql": sql,
        "columns": columns or [],
        "rows": [],
        "chart": {
            "type": "none",
            "title": "未生成图表",
            "x_field": None,
            "y_field": None,
            "series_name": None,
            "series_field": None,
            "series_fields": [],
        },
        "insights": [message],
        "knowledge_refs": knowledge or [],
        "execution_mode": "business-feedback",
        "analysis_engine": "none",
        "answer_type": "data_analysis",
        "context_applied": context_applied,
        "effective_question": effective_question or question,
    }


async def _answer_knowledge(
    question: str,
    effective_question: str,
    session_id: str,
    dataset_id: int | None,
    history: list[dict[str, Any]],
    context_applied: bool,
    intent_result: IntentResult,
) -> dict:
    settings = get_settings()
    knowledge = await search_knowledge(effective_question, dataset_id, limit=5)
    answer = await answer_from_knowledge(question, knowledge, history)
    execution_mode = "llm-assisted" if settings.llm_configured else "knowledge-extract"
    plan_steps = build_plan_steps(
        intent=intent_result,
        answer_type="knowledge_qa",
        execution_mode=execution_mode,
    )
    payload = {
        "session_id": session_id,
        "message": "已根据企业知识库回答。",
        "intent": intent_result.display_name,
        "intent_label": intent_result.label,
        "intent_confidence": intent_result.confidence,
        "intent_method": intent_result.method,
        "intent_reason": intent_result.reason,
        "plan": plan_titles(plan_steps),
        "plan_steps": plan_steps,
        "sql": "",
        "columns": [],
        "rows": [],
        "chart": {
            "type": "none",
            "title": "企业知识库回答",
            "x_field": None,
            "y_field": None,
            "series_name": None,
            "series_field": None,
            "series_fields": [],
        },
        "insights": [answer],
        "knowledge_refs": knowledge,
        "execution_mode": execution_mode,
        "answer_type": "knowledge_qa",
        "context_applied": context_applied,
        "effective_question": effective_question,
    }
    _store_assistant(session_id, payload, "knowledge_qa", dataset_id)
    return payload


async def _anomaly_attribution_insights(
    question: str,
    dataset: dict,
    query_plan: QueryPlan,
    rows: list[dict[str, Any]],
) -> list[str]:
    """Multi-dimensional attribution: compare periods + drill down by region/product/channel/time."""

    if not rows or not query_plan.time_description:
        return _draft_insights(rows, query_plan.x_field, query_plan.y_field, query_plan.series_fields)

    # 1. Build comparison query for previous period
    prev_desc_map = {
        "近": "上期", "最近": "上期", "本月": "上月", "今年": "去年", "本年": "去年",
    }
    prev_question = question
    for curr, prev in prev_desc_map.items():
        if curr in question:
            prev_question = question.replace(curr, prev, 1)
            break
    if prev_question == question:
        prev_question = f"上期 {question}"

    prev_plan = _build_query(prev_question, dataset, max(len(rows) * 2, 100))
    try:
        with connect() as conn:
            prev_result = conn.execute(prev_plan.sql, prev_plan.params).fetchall()
            prev_rows = [dict(row) for row in prev_result]
    except Exception:
        return _draft_insights(rows, query_plan.x_field, query_plan.y_field, query_plan.series_fields)

    if not prev_rows:
        return _draft_insights(rows, query_plan.x_field, query_plan.y_field, query_plan.series_fields)

    # 2. Calculate overall delta
    y_field = query_plan.y_field
    current_total = sum(float(row.get(y_field, 0) or 0) for row in rows)
    prev_total = sum(float(row.get(y_field, 0) or 0) for row in prev_rows)
    if prev_total == 0:
        return _draft_insights(rows, query_plan.x_field, query_plan.y_field, query_plan.series_fields)

    delta_pct = round((current_total - prev_total) / prev_total * 100, 1)

    # 3. Drill down: region → product → channel → time (month/week)
    profile = _column_profile(dataset["columns"])
    dimensions: list[tuple[str, str]] = []
    if profile.get("region") and any(row.get(profile["region"]) for row in rows if profile["region"]):
        dimensions.append((profile["region"], "区域"))
    if profile.get("product") and any(row.get(profile["product"]) for row in rows if profile["product"]):
        dimensions.append((profile["product"], "产品类别"))
    if profile.get("channel") and any(row.get(profile["channel"]) for row in rows if profile["channel"]):
        dimensions.append((profile["channel"], "渠道"))
    # 4th dimension: time-based segmentation
    if profile.get("date") and query_plan.x_field not in ("区域", "产品类别", "渠道"):
        dimensions.append((profile["date"], "时间"))

    contributions: list[dict] = []
    for dim_col, dim_label in dimensions:
        current_by: dict[str, float] = defaultdict(float)
        prev_by: dict[str, float] = defaultdict(float)
        for row in rows:
            dim_val = str(row.get(dim_col, "未知"))[:16]
            current_by[dim_val] += float(row.get(y_field, 0) or 0)
        for row in prev_rows:
            dim_val = str(row.get(dim_col, "未知"))[:16]
            prev_by[dim_val] += float(row.get(y_field, 0) or 0)

        all_vals = set(current_by.keys()) | set(prev_by.keys())
        for val in all_vals:
            curr = current_by.get(val, 0)
            prev = prev_by.get(val, 0)
            if prev == 0:
                continue
            dim_delta = round((curr - prev) / prev * 100, 1)
            contrib = round((curr - prev) / prev_total * 100, 1)
            contributions.append({
                "dimension": dim_label,
                "value": val,
                "current": round(curr, 2),
                "previous": round(prev, 2),
                "delta_pct": dim_delta,
                "contribution_pct": contrib,
            })

    contributions.sort(key=lambda x: abs(x["contribution_pct"]), reverse=True)

    # 4. Format insights
    insights: list[str] = []
    direction = "增长" if delta_pct > 0 else "下降"
    insights.append(f"本期{y_field}较上期{direction}{abs(delta_pct)}%（{prev_total:,.2f}→{current_total:,.2f}）。")

    if contributions:
        for item in contributions[:4]:
            dw = "增长" if item["delta_pct"] > 0 else "下降"
            insights.append(
                f"• {item['dimension']}「{item['value']}」{dw}{abs(item['delta_pct'])}%，"
                f"贡献总变化的{abs(item['contribution_pct'])}%（{item['previous']:,.2f}→{item['current']:,.2f}）"
            )
        largest = contributions[0]
        insights.append(f"→ 建议重点关注{largest['dimension']}「{largest['value']}」的异常变化，排查业务根因。")

    return insights


async def analyze_react(question: str, session_id: str | None, dataset_id: int | None, *, use_mcp: bool = False) -> dict:
    """ReAct Agent path — LLM-driven tool calling loop with memory integration."""
    settings = get_settings()
    session_id = session_id or uuid.uuid4().hex
    history = _load_history(session_id)

    # M-003: LLM-based follow-up merging
    merged_question, is_followup = await llm_merge_followup(question, history)
    effective_question = merged_question

    # M-001: compress old history
    compressed = await compress_history(history) if len(history) > 3 else ""

    try:
        dataset = _dataset(dataset_id)
    except ValueError as exc:
        intent_result = IntentResult("data_query", 0.8, "rules", "没有可用数据源")
        message = f"{exc}。请先在“数据源”页面上传或导入数据，然后再发起分析。"
        _store_user(session_id, question, dataset_id)
        payload = _business_feedback_payload(
            session_id=session_id,
            question=question,
            effective_question=effective_question,
            message=message,
            intent_result=intent_result,
            dataset=None,
            context_applied=is_followup,
        )
        _store_assistant(session_id, payload, "analyze", dataset_id)
        return payload

    # Inject compressed context into question for ReAct agent
    react_question = effective_question
    if compressed:
        react_question = f"[对话背景]\n{compressed}\n\n[当前问题] {effective_question}"

    # M-002: load user profile
    actor_id = _actor_id_from_history(history)
    user_profile = await load_user_profile(actor_id) if actor_id else {}
    if user_profile:
        profile_ctx = format_profile_context(user_profile)
        if profile_ctx:
            react_question = f"{profile_ctx}\n{react_question}"

    # Semantic cache: check before expensive LLM calls
    cached = await cache_lookup(question)
    if cached:
        cached["session_id"] = session_id
        cached["execution_mode"] = cached.get("execution_mode", "cached")
        cached["_from_cache"] = True
        return cached

    react_result = await run_react_loop(
        question=react_question,
        dataset_id=dataset["id"],
        table_name=dataset["table_name"],
        columns=dataset["columns"],
        history=history,
        use_mcp=use_mcp,
    )
    react_result["session_id"] = session_id
    react_result["context_applied"] = is_followup or bool(compressed)
    react_result["effective_question"] = effective_question
    _store_user(session_id, question, dataset["id"])
    _store_assistant(session_id, react_result, "analyze", dataset["id"])

    # Cache the result for similar future queries
    await cache_store(question, react_result)

    # M-002: update user profile after analysis
    if actor_id:
        await update_user_profile_from_analysis(
            actor_id, question,
            react_result.get("insights", []),
            dataset["name"],
        )

    return react_result


async def analyze_with_document(
    question: str,
    session_id: str | None,
    dataset_id: int | None,
    *,
    filename: str,
    file_content: bytes,
) -> dict:
    """Analyze an uploaded file.

    Default behavior is file-first analysis.  The relational dataset is used
    only when the user explicitly asks to combine the uploaded file with the
    current database/data source.
    """
    from .knowledge_documents import extract_document_text

    session_id = session_id or uuid.uuid4().hex
    history = _load_history(session_id)

    # Parse the document
    try:
        doc_text = extract_document_text(filename, file_content)
    except ValueError as exc:
        raise ValueError(f"文档解析失败：{exc}") from exc

    doc_title = filename.rsplit(".", 1)[0] if "." in filename else filename
    effective_question = question or f"分析文档: {doc_title}"
    stored_document = save_session_document(
        session_id=session_id,
        filename=filename,
        content=file_content,
        extracted_text=doc_text,
        dataset_id=dataset_id,
    )

    if not wants_database_context(effective_question):
        document_result = await analyze_uploaded_document(
            session_id=session_id,
            filename=filename,
            text=doc_text,
            question=effective_question,
            history=history,
        )
        document_result["document_analyzed"] = doc_title
        document_result["document_context_applied"] = True
        document_result["session_documents"] = [
            {
                "id": stored_document.get("id"),
                "filename": stored_document.get("filename"),
                "file_type": stored_document.get("file_type"),
                "file_size": stored_document.get("file_size"),
            }
        ]
        update_session_document_summary(
            int(stored_document["id"]),
            document_result.get("message") or "\n".join(document_result.get("insights") or []),
        )
        _store_user(session_id, effective_question, dataset_id)
        _store_assistant(session_id, document_result, "document_analyze", dataset_id)
        return document_result

    try:
        dataset = _dataset(dataset_id)
    except ValueError as exc:
        intent_result = IntentResult("knowledge_qa", 0.8, "rules", "用户要求结合数据库，但没有可用数据源")
        message = f"{exc}。我已收到文件，但你要求结合数据库分析；请先在“数据源”页面上传或导入数据，或改为只分析文件。"
        _store_user(session_id, effective_question, dataset_id)
        payload = _business_feedback_payload(
            session_id=session_id,
            question=question,
            effective_question=effective_question,
            message=message,
            intent_result=intent_result,
            dataset=None,
            context_applied=False,
        )
        _store_assistant(session_id, payload, "document_analyze", dataset_id)
        return payload

    # Build document-augmented question
    augmented_question = (
        f"[上传文档] {doc_title}\n"
        f"[文档内容摘要]\n{doc_text[:6000]}\n\n"
        f"[用户问题] {effective_question}\n\n"
        "重要要求：用户明确要求结合数据源时才允许查询数据库。回答时必须区分“文件内容结论”和“数据库查询结论”。"
    )

    # Store document knowledge temporarily for RAG
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_chunks(title, content, category, dataset_id) VALUES (?, ?, ?, ?)",
            (f"临时文档: {doc_title}", doc_text[:2000], "business_rule", dataset["id"]),
        )
        temp_knowledge_id = cursor.lastrowid

    try:
        # Run analysis with document context
        react_result = await run_react_loop(
            question=augmented_question,
            dataset_id=dataset["id"],
            table_name=dataset["table_name"],
            columns=dataset["columns"],
            history=history,
        )
    finally:
        # Clean up temporary knowledge
        if temp_knowledge_id:
            with connect() as conn:
                conn.execute("DELETE FROM knowledge_chunks WHERE id = ?", (temp_knowledge_id,))

    react_result["session_id"] = session_id
    react_result["document_analyzed"] = doc_title
    react_result["document_context_applied"] = True
    react_result["session_documents"] = [
        {
            "id": stored_document.get("id"),
            "filename": stored_document.get("filename"),
            "file_type": stored_document.get("file_type"),
            "file_size": stored_document.get("file_size"),
        }
    ]
    update_session_document_summary(
        int(stored_document["id"]),
        react_result.get("message") or "\n".join(react_result.get("insights") or []),
    )
    _store_user(session_id, question or f"分析文档: {doc_title}", dataset["id"])
    _store_assistant(session_id, react_result, "analyze", dataset["id"])
    return react_result


async def analyze_to_report_stream(
    question: str,
    session_id: str | None,
    dataset_id: int | None,
    *,
    filename: str = "",
    file_content: bytes | None = None,
):
    """Phase 2: Full report pipeline — plan → execute sections → yield SSE events.

    Yields SSE event dicts: plan_start, section_start, section_done, plan_done, error.
    """
    settings = get_settings()
    session_id = session_id or uuid.uuid4().hex
    history = _load_history(session_id)

    # If file uploaded, parse and add context
    effective_question = question
    if file_content and filename:
        from .knowledge_documents import extract_document_text
        try:
            doc_text = extract_document_text(filename, file_content)
            effective_question = f"[文档内容: {filename}]\n{doc_text[:4000]}\n\n[用户问题] {question or '请全面分析这份数据'}"
        except Exception:
            pass

    dataset = _dataset(dataset_id)
    _store_user(session_id, question or "分析报告", dataset["id"])

    # Phase 0: Intent + Persona
    data_profile = profile_dataset(dataset["id"])
    intent = await route_intent(effective_question, data_profile)

    # Phase 1: Analysis Plan
    plan = await plan_analysis(effective_question, data_profile, intent["matched_persona"])
    yield {"type": "plan", "plan": plan, "persona": intent["matched_persona"], "clarification": intent.get("clarification")}

    if not intent.get("info_sufficient") and intent.get("clarification"):
        yield {"type": "need_clarification", "message": intent["clarification"].get("message", ""), "options": intent["clarification"].get("options", [])}
        return

    # Phase 2: Execute sections with SSE streaming
    insights_collected: list[str] = []
    all_rows: list[dict] = []
    chart_meta = {"type": "bar", "title": plan.get("report_title", "分析报告")}
    chart_sections: list[dict[str, Any]] = []

    async for event in execute_plan_stream(
        plan, dataset["table_name"], dataset["columns"], effective_question, dataset["name"],
    ):
        if event["type"] == "section_done":
            insights_collected.append(event.get("narrative", ""))
            if event.get("rows"):
                section_rows = [_jsonable_row(dict(row)) for row in event["rows"][:50]]
                all_rows.extend(section_rows[:10])
                x_field, y_field = _infer_chart_fields(section_rows)
                chart_type = event.get("chart_type") or "bar"
                if chart_type in {"table", "kpi_cards"}:
                    chart_type = "bar"
                if x_field and y_field:
                    section_chart = {
                        "type": chart_type if chart_type in {"bar", "line", "pie", "scatter", "area", "radar"} else "bar",
                        "title": event.get("title") or "分析图表",
                        "x_field": x_field,
                        "y_field": y_field,
                        "series_name": y_field,
                        "series_field": None,
                        "series_fields": [],
                        "recommendation": {
                            "type": chart_type if chart_type in {"bar", "line", "pie", "scatter", "area", "radar"} else "bar",
                            "source": "report-pipeline",
                            "reason": "多维报告管线为该分析章节生成的图表。",
                            "confidence": 0.74,
                        },
                    }
                    chart_sections.append(
                        _chart_section(
                            section_id=f"report-section-{event.get('index') or len(chart_sections) + 1}",
                            title=event.get("title") or "分析章节",
                            description=event.get("narrative") or "",
                            rows=section_rows,
                            chart=section_chart,
                            insights=[event.get("narrative", "")] if event.get("narrative") else [],
                        )
                    )
        yield event

    if chart_sections:
        first = chart_sections[0]
        chart_meta = first["chart"]

    # Build final result payload
    payload = {
        "session_id": session_id,
        "message": f"分析报告生成完成，共 {len(insights_collected)} 个分析维度。",
        "intent": intent.get("task_type", "analysis"),
        "plan": plan.get("sections", []),
        "sql": "",
        "columns": chart_sections[0]["columns"] if chart_sections else (list(all_rows[0].keys()) if all_rows else []),
        "rows": chart_sections[0]["rows"] if chart_sections else all_rows[:50],
        "chart": chart_meta,
        "chart_sections": chart_sections,
        "insights": insights_collected,
        "knowledge_refs": [],
        "execution_mode": "report-pipeline",
        "answer_type": "data_analysis",
        "context_applied": False,
        "effective_question": effective_question,
    }
    _store_assistant(session_id, payload, "analyze", dataset["id"])
    yield {"type": "result", "data": payload}


async def analyze(question: str, session_id: str | None, dataset_id: int | None) -> dict:
    """Non-streaming analysis: runs the full pipeline and returns the result dict."""
    result = None
    last_error = None
    async for event in analyze_stream(question, session_id, dataset_id):
        if event["type"] == "result":
            result = event["data"]
        elif event["type"] == "error":
            last_error = event.get("message") or event.get("error")
        elif event["type"] == "done" and event.get("error"):
            last_error = event.get("error")
    if result is None:
        raise ValueError(last_error or "分析流程未返回结果")
    return result


async def analyze_stream(question: str, session_id: str | None, dataset_id: int | None):
    """Streaming analysis: yields SSE events as the pipeline progresses."""
    settings = get_settings()
    session_id = session_id or uuid.uuid4().hex
    history = _load_history(session_id)
    effective_question, context_applied = await llm_merge_followup(question, history)
    session_doc_context = load_session_document_context(session_id, effective_question)

    if should_use_session_documents(effective_question, history, session_doc_context):
        _store_user(session_id, question, dataset_id)
        plan_step_titles = ["识别追问意图", "读取会话已上传文件", "检索相关文件片段", "生成文件上下文回答"]
        yield {"type": "plan", "steps": plan_step_titles, "intent": "文件追问", "answer_type": "knowledge_qa"}
        yield {"type": "step", "step_id": 1, "title": plan_step_titles[0], "status": "completed", "detail": "检测到当前会话存在已上传文件"}
        yield {"type": "step", "step_id": 2, "title": plan_step_titles[1], "status": "completed", "detail": f"{len(session_doc_context.get('documents') or [])} 个文件"}
        yield {"type": "step", "step_id": 3, "title": plan_step_titles[2], "status": "completed", "detail": f"{len(session_doc_context.get('chunks') or [])} 个相关片段"}
        yield {"type": "thinking", "content": "已将当前会话上传过的文件作为上下文，正在结合相关片段回答本轮追问..."}
        document_result = await analyze_uploaded_document(
            session_id=session_id,
            filename=session_doc_context.get("virtual_filename") or "会话文件上下文.md",
            text=session_doc_context.get("combined_text") or "",
            question=effective_question,
            history=history,
        )
        document_result["document_context_applied"] = True
        document_result["context_applied"] = True
        document_result["session_documents"] = [
            {
                "id": item.get("id"),
                "filename": item.get("filename"),
                "file_type": item.get("file_type"),
                "file_size": item.get("file_size"),
            }
            for item in (session_doc_context.get("documents") or [])
        ]
        if session_doc_context.get("knowledge_refs"):
            document_result["knowledge_refs"] = session_doc_context["knowledge_refs"]
        document_result["message"] = document_result.get("message") or "已根据当前会话上传文件完成分析。"
        _store_assistant(session_id, document_result, "document_followup", dataset_id)
        yield {"type": "step", "step_id": 4, "title": plan_step_titles[3], "status": "completed", "detail": "回答已生成"}
        yield {"type": "result", "data": document_result}
        yield {"type": "done"}
        return

    # Step 1: Intent classification
    intent_result = await classify_intent(effective_question, history)
    force_database_analysis = _should_force_database_analysis(effective_question)
    if force_database_analysis:
        intent_result = IntentResult(
            "data_query",
            max(intent_result.confidence, 0.9),
            "rules",
            "问题明确指向当前数据库/数据源，应走数据分析而不是知识库问答",
        )
    if not force_database_analysis and _is_knowledge_question(question, history) and intent_result.label == "data_query":
        intent_result = IntentResult("knowledge_qa", 0.86, "rules", "知识问答追问兜底")
    _store_user(session_id, question, dataset_id)

    # Emit plan early so the frontend can show steps immediately
    plan_step_titles = _build_stream_plan_titles(intent_result, "knowledge_qa" if intent_result.label == "knowledge_qa" else "data_analysis")
    yield {"type": "plan", "steps": plan_step_titles, "intent": intent_result.display_name, "answer_type": intent_result.label}

    # Knowledge QA path
    if intent_result.label == "knowledge_qa":
        yield {"type": "step", "step_id": 1, "title": plan_step_titles[0] if len(plan_step_titles) > 0 else "意图识别", "status": "running"}
        yield {"type": "thinking", "content": f"识别意图为：{intent_result.display_name}，置信度 {intent_result.confidence:.0%}"}
        yield {"type": "step", "step_id": 1, "title": plan_step_titles[0] if len(plan_step_titles) > 0 else "意图识别", "status": "completed", "detail": intent_result.display_name}

        yield {"type": "step", "step_id": 2, "title": plan_step_titles[1] if len(plan_step_titles) > 1 else "检索知识库", "status": "running"}
        yield {"type": "thinking", "content": "正在从知识库中检索相关业务知识..."}
        knowledge = await search_knowledge(effective_question, dataset_id, limit=5)
        yield {"type": "step", "step_id": 2, "title": plan_step_titles[1] if len(plan_step_titles) > 1 else "检索知识库", "status": "completed", "detail": f"找到 {len(knowledge)} 条相关知识"}

        yield {"type": "step", "step_id": 3, "title": plan_step_titles[2] if len(plan_step_titles) > 2 else "生成回答", "status": "running"}
        yield {"type": "thinking", "content": f"正在结合 {len(knowledge)} 条业务知识生成回答..."}
        answer = await answer_from_knowledge(question, knowledge, history)
        yield {"type": "step", "step_id": 3, "title": plan_step_titles[2] if len(plan_step_titles) > 2 else "生成回答", "status": "completed", "detail": "回答已生成"}

        execution_mode = "llm-assisted" if settings.llm_configured else "knowledge-extract"
        plan_steps = build_plan_steps(
            intent=intent_result,
            answer_type="knowledge_qa",
            execution_mode=execution_mode,
        )
        payload = {
            "session_id": session_id,
            "message": "已根据企业知识库回答。",
            "intent": intent_result.display_name,
            "intent_label": intent_result.label,
            "intent_confidence": intent_result.confidence,
            "intent_method": intent_result.method,
            "intent_reason": intent_result.reason,
            "plan": plan_titles(plan_steps),
            "plan_steps": plan_steps,
            "sql": "",
            "columns": [],
            "rows": [],
            "chart": {
                "type": "none",
                "title": "企业知识库回答",
                "x_field": None,
                "y_field": None,
                "series_name": None,
                "series_field": None,
                "series_fields": [],
            },
            "insights": [answer],
            "knowledge_refs": knowledge,
            "execution_mode": execution_mode,
            "answer_type": "knowledge_qa",
            "context_applied": context_applied,
            "effective_question": effective_question,
        }
        _store_assistant(session_id, payload, "knowledge_qa", dataset_id)
        yield {"type": "result", "data": payload}
        yield {"type": "done"}
        return

    # Data analysis path
    yield {"type": "step", "step_id": 1, "title": plan_step_titles[0] if len(plan_step_titles) > 0 else "意图识别", "status": "running"}
    yield {"type": "thinking", "content": f"识别意图为：{intent_result.display_name}，置信度 {intent_result.confidence:.0%}"}
    yield {"type": "step", "step_id": 1, "title": plan_step_titles[0] if len(plan_step_titles) > 0 else "意图识别", "status": "completed", "detail": intent_result.display_name}

    yield {"type": "step", "step_id": 2, "title": plan_step_titles[1] if len(plan_step_titles) > 1 else "分析数据集", "status": "running"}
    try:
        dataset = _dataset(dataset_id)
    except ValueError as exc:
        message = f"{exc}。请先在“数据源”页面上传或导入数据，然后再发起分析。"
        payload = _business_feedback_payload(
            session_id=session_id,
            question=question,
            effective_question=effective_question,
            message=message,
            intent_result=intent_result,
            dataset=None,
            context_applied=context_applied,
        )
        _store_assistant(session_id, payload, "analyze", dataset_id)
        yield {"type": "step", "step_id": 2, "title": "分析数据源", "status": "completed", "detail": "没有可用数据源，已生成说明"}
        yield {"type": "result", "data": payload}
        yield {"type": "done"}
        return
    dataset_id = dataset["id"]
    yield {"type": "thinking", "content": f"使用数据集「{dataset['name']}」，包含 {len(dataset['columns'])} 个字段、约 {dataset.get('row_count', '?')} 条数据"}
    yield {"type": "step", "step_id": 2, "title": plan_step_titles[1] if len(plan_step_titles) > 1 else "分析数据集", "status": "completed", "detail": dataset["name"]}

    generic_result = _try_generic_data_exploration(effective_question, dataset, settings.query_row_limit)
    if generic_result:
        yield {"type": "step", "step_id": 3, "title": "通用数据探索", "status": "running"}
        mode_label = {
            "table_overview": "表结构与内容概览",
            "generic_topn": "通用排名查询",
            "database_intelligence": "数据库智能探索",
        }.get(str(generic_result.get("mode") or ""), "通用数据探索")
        yield {"type": "thinking", "content": f"识别为「{mode_label}」问题，正在自动读取表结构、样例数据或构建通用排名 SQL..."}
        query_plan = generic_result["query_plan"]
        rows = generic_result["rows"]
        chart = generic_result["chart"]
        insights = generic_result["insights"]
        chart_sections = generic_result.get("chart_sections") or []
        omitted_chart_sections = generic_result.get("omitted_chart_sections") or []
        field_corrections: list[dict[str, str]] = []
        if chart.get("type") != "none":
            query_plan, field_corrections = _coerce_query_plan_numeric_fields(rows, query_plan, dataset)
        if field_corrections:
            chart = {
                **chart,
                "x_field": query_plan.x_field,
                "y_field": query_plan.y_field,
                "series_name": query_plan.y_field,
                "series_field": query_plan.series_field,
                "series_fields": query_plan.series_fields,
            }
        insights, chart_sections, narrative_applied = await _apply_database_business_narrative(
            question=effective_question,
            dataset=dataset,
            rows=rows,
            insights=insights,
            knowledge=generic_result.get("knowledge_refs") or [],
            chart_sections=chart_sections,
            omitted_chart_sections=omitted_chart_sections,
            technical_notes=[],
        )
        plan_steps = build_plan_steps(
            intent=intent_result,
            answer_type="data_analysis",
            execution_mode="generic-data-explorer",
            scope=query_plan.x_field,
            dataset_name=dataset["name"],
            chart_type=chart.get("type") or "none",
        )
        payload = {
            "session_id": session_id,
            "message": generic_result.get("message") or "已完成通用数据探索。",
            "intent": "数据探索",
            "intent_label": intent_result.label,
            "intent_confidence": intent_result.confidence,
            "intent_method": intent_result.method,
            "intent_reason": intent_result.reason,
            "sql_source": generic_result.get("plan_source"),
            "plan": plan_titles(plan_steps),
            "plan_steps": plan_steps,
            "sql": query_plan.sql,
            "columns": generic_result["columns"],
            "rows": rows,
            "chart": chart,
            "chart_sections": chart_sections,
            "omitted_chart_sections": omitted_chart_sections,
            "insights": insights,
            "knowledge_refs": generic_result.get("knowledge_refs") or [],
            "execution_mode": "database-intelligence" if generic_result.get("mode") == "database_intelligence" else "generic-data-explorer",
            "analysis_engine": "database_intelligence" if generic_result.get("mode") == "database_intelligence" else "generic_sql",
            "sql_repair": None,
            "field_mappings": query_plan.field_mappings,
            "answer_type": "data_analysis",
            "context_applied": context_applied,
            "effective_question": effective_question,
            "sample_rows": generic_result.get("sample_rows") or [],
            "dataset_domain": generic_result.get("domain"),
            "narrative_applied": narrative_applied,
        }
        _store_assistant(session_id, payload, "generic_data_explore", dataset_id)
        yield {"type": "step", "step_id": 3, "title": "通用数据探索", "status": "completed", "detail": mode_label}
        yield {"type": "result", "data": payload}
        yield {"type": "done"}
        return

    # 提前检索业务知识，供 SQL 生成和洞察润色共用（Q-001: RAG→SQL 数据流）
    knowledge = await search_knowledge(effective_question, dataset_id)

    yield {"type": "step", "step_id": 3, "title": plan_step_titles[2] if len(plan_step_titles) > 2 else "构建查询", "status": "running"}
    query_plan = _build_query(effective_question, dataset, settings.query_row_limit)
    knowledge_hint = f"，结合 {len(knowledge)} 条业务知识优化 SQL..." if knowledge else ""
    yield {"type": "thinking", "content": f"正在根据表 {dataset['table_name']} 的字段生成 SQL 查询{knowledge_hint}"}
    try:
        safe_sql, rows, plan_source, sql_repair = await _execute_sql_with_repair(
            effective_question,
            dataset,
            query_plan,
            settings.query_row_limit,
            business_knowledge=knowledge,        # Q-001: RAG 注入 SQL 生成
            intent_reason=intent_result.reason,  # Q-004: 意图推理上下文注入 SQL 生成
        )
        query_plan, field_corrections = _coerce_query_plan_numeric_fields(rows, query_plan, dataset)
        if field_corrections and isinstance(sql_repair, dict):
            sql_repair["field_corrections"] = field_corrections
    except FieldNotFoundError as e:
        message = f"{e.message}。这通常表示当前数据源中没有用户问题提到的字段、指标或业务对象。请换一个数据源、检查字段说明，或在业务知识库中补充对应指标口径。"
        payload = _business_feedback_payload(
            session_id=session_id,
            question=question,
            effective_question=effective_question,
            message=message,
            intent_result=intent_result,
            dataset=dataset,
            knowledge=knowledge,
            context_applied=context_applied,
        )
        _store_assistant(session_id, payload, "analyze", dataset_id)
        yield {"type": "step", "step_id": 3, "title": plan_step_titles[2] if len(plan_step_titles) > 2 else "构建查询", "status": "completed", "detail": "字段或指标不存在，已生成说明"}
        yield {"type": "result", "data": payload}
        yield {"type": "done"}
        return
    yield {"type": "step", "step_id": 3, "title": plan_step_titles[2] if len(plan_step_titles) > 2 else "构建查询", "status": "completed", "detail": f"返回 {len(rows)} 条结果"}

    if not rows:
        message = (
            "查询已执行，但当前条件下没有匹配到数据。"
            "可能原因是时间范围、地区/产品等筛选条件没有对应记录，或者当前数据源不包含这类业务数据。"
            "你可以放宽筛选条件、换一个数据源，或先在“数据源”页面查看字段和数据预览。"
        )
        payload = _business_feedback_payload(
            session_id=session_id,
            question=question,
            effective_question=effective_question,
            message=message,
            intent_result=intent_result,
            dataset=dataset,
            sql=safe_sql,
            columns=[query_plan.x_field, query_plan.y_field],
            knowledge=knowledge,
            context_applied=context_applied,
        )
        _store_assistant(session_id, payload, "analyze", dataset_id)
        yield {"type": "step", "step_id": 4, "title": "生成说明", "status": "completed", "detail": "没有匹配数据，已生成说明"}
        yield {"type": "result", "data": payload}
        yield {"type": "done"}
        return

    yield {"type": "step", "step_id": 4, "title": plan_step_titles[3] if len(plan_step_titles) > 3 else "生成洞察", "status": "running"}
    draft = _draft_insights(rows, query_plan.x_field, query_plan.y_field, query_plan.series_fields)
    yield {"type": "thinking", "content": f"查询返回 {len(rows)} 条结果" + (f"，结合 {len(knowledge)} 条业务知识提炼洞察..." if knowledge else "...")}
    if intent_result.label == "anomaly_attribution":
        insights = await _anomaly_attribution_insights(effective_question, dataset, query_plan, rows)
    else:
        insights = await polish_insights(
            effective_question, rows, draft, knowledge,
            intent_reason=intent_result.reason,   # Q-004: 意图推理上下文注入洞察润色
            plan_source=plan_source,               # Q-004: SQL 生成来源注入洞察润色
        )
    chart_recommendation = recommend_chart(
        effective_question,
        intent_result.label,
        rows,
        query_plan.x_field,
        query_plan.y_field,
        query_plan.series_fields,
    )
    chart_type = chart_recommendation["type"]
    yield {"type": "step", "step_id": 4, "title": plan_step_titles[3] if len(plan_step_titles) > 3 else "生成洞察", "status": "completed", "detail": f"{len(insights)} 条洞察"}

    intent = intent_result.display_name
    scope_parts = [item for item in (query_plan.time_description, *query_plan.series_fields) if item]
    scope = "、".join(scope_parts) or query_plan.x_field
    execution_mode = "llm-assisted" if settings.llm_configured else "local-demo"
    chart = {
        "type": chart_type,
        "title": f"{dataset['name']} - {(query_plan.time_description + ' ') if query_plan.time_description else ''}{query_plan.y_field}",
        "x_field": query_plan.x_field,
        "y_field": query_plan.y_field,
        "series_name": query_plan.y_field,
        "series_field": query_plan.series_field,
        "series_fields": query_plan.series_fields,
        "recommendation": chart_recommendation,
        "alternatives": chart_recommendation.get("alternatives", []),
        "display_mode": chart_recommendation.get("display_mode", "single"),
        "secondary_y_field": chart_recommendation.get("secondary_y_field"),
        "facet_fields": chart_recommendation.get("facet_fields", []),
    }
    python_analysis: dict[str, Any] | None = None
    python_code: str | None = None
    analysis_engine = "sql"
    if settings.llm_configured and _needs_python_analysis(effective_question, intent_result.label):
        yield {"type": "step", "step_id": 5, "title": "深度分析 (Python/Pandas)", "status": "running"}
        yield {"type": "thinking", "content": "正在使用 Python/Pandas 进行深度分析..."}
        python_result = await execute_python_analysis(
            effective_question,
            dataset["table_name"],
            dataset["columns"],
            int(dataset.get("row_count") or 0),
            knowledge,
        )
        python_code = python_result.get("code") or ""
        python_payload = python_result.get("result") if isinstance(python_result.get("result"), dict) else None
        python_analysis = {
            "success": bool(python_result.get("success")),
            "error": python_result.get("error"),
            "traceback": python_result.get("traceback"),
            "result": python_payload,
            "repair_stats": python_result.get("repair_stats"),
        }
        if python_result.get("success") and python_payload:
            analysis_engine = "python_pandas"
            execution_mode = "llm-python-pandas"
            summary = python_payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                insights = [summary.strip()]
            generated_rows = _python_rows(python_payload.get("data"))
            if generated_rows:
                rows = generated_rows
            chart = _apply_python_chart(chart, python_payload.get("chart_suggestion"))
            chart_type = chart["type"]
        yield {"type": "step", "step_id": 5, "title": "深度分析 (Python/Pandas)", "status": "completed", "detail": "完成" if python_analysis and python_analysis["success"] else "未执行"}
        yield {"type": "thinking", "content": "已使用 Python/Pandas 完成深度分析" if (python_analysis and python_analysis["success"]) else "Python 分析未执行，使用 SQL 结果"}

    technical_notes: list[str] = []
    fallback_message = sql_repair.get("field_not_found_fallback") if isinstance(sql_repair, dict) else None
    if fallback_message:
        technical_notes.append(
            "字段口径说明：模型没有在数据表中精确找到用户提到的字段，"
            f"已先按当前可用字段「{query_plan.x_field} / {query_plan.y_field}」生成近似分析；"
            "如果口径不符合预期，可以继续指定字段名或补充业务说明。"
        )

    alias_mapping = sql_repair.get("column_alias_mapping") if isinstance(sql_repair, dict) else None
    if isinstance(alias_mapping, dict) and alias_mapping:
        alias_note = "字段别名修正：" + "；".join(
            f"已将 SQL 返回列「{actual}」按「{expected}」使用"
            for expected, actual in alias_mapping.items()
            if expected != actual
        )
        if alias_note != "字段别名修正：":
            technical_notes.append(alias_note)

    mapping_notes = field_mapping_notes(query_plan.field_mappings)
    if mapping_notes:
        technical_notes.extend(mapping_notes)

    chart_sections, omitted_chart_sections = _build_data_chart_sections(
        question=effective_question,
        intent_label=intent_result.label,
        dataset=dataset,
        rows=rows,
        chart=chart,
        query_plan=query_plan,
        limit=settings.query_row_limit,
    )

    # ══════════════════════════════════════════════════════════════════════
    # A-003 / A-005: Anchored Lightweight Reflection
    # ──────────────────────────────────────────────────────────────────────
    # Three safety mechanisms:
    #   ① Immutable anchors — only check 3 extracted must-answer points
    #   ② Hard limit — 1 round reflect + 1 round patch; fallback to original
    #   ③ Delta fix — patch is APPENDED, existing content is never rewritten
    # ══════════════════════════════════════════════════════════════════════
    reflection_applied = False
    if settings.llm_configured and insights:
        try:
            anchors = _extract_anchors(effective_question)
            if anchors:
                yield {"type": "thinking", "content": f"🔍 质量检查中（锚点：{'、'.join(anchors)}）..."}
                reflection = await reflect_on_insights(
                    effective_question, anchors, insights, rows, knowledge
                )
                if reflection.get("has_gaps") and reflection.get("anchors_missing"):
                    missing_desc = reflection.get("gap_description", "部分维度未覆盖")
                    yield {"type": "thinking", "content": f"⚠ 洞察缺失：{missing_desc}，正在补充..."}
                    patch = await patch_insights(
                        effective_question,
                        reflection["anchors_missing"],
                        reflection.get("gap_description", ""),
                        insights,
                        rows,
                        knowledge,
                    )
                    if patch:
                        insights = insights + patch  # ← APPEND, never rewrite!
                        reflection_applied = True
                        yield {"type": "thinking", "content": f"✅ 已补充 {len(patch)} 条洞察：{patch[0][:60]}..."}
                else:
                    yield {"type": "thinking", "content": "✅ 洞察质量检查通过，所有锚点已覆盖"}
        except Exception as reflection_exc:
            import logging
            logging.getLogger(__name__).warning("Reflection failed, using original insights: %s", reflection_exc)
            yield {"type": "thinking", "content": f"⚡ 质量检查跳过（{str(reflection_exc)[:60]}），使用原始洞察"}  # Graceful degradation

    yield {"type": "thinking", "content": "正在把查询结果转成业务解释，隐藏内部字段和 SQL 处理细节..."}
    insights, chart_sections, narrative_applied = await _apply_database_business_narrative(
        question=effective_question,
        dataset=dataset,
        rows=rows,
        insights=insights,
        knowledge=knowledge,
        chart_sections=chart_sections,
        omitted_chart_sections=omitted_chart_sections,
        technical_notes=technical_notes,
    )

    plan_steps = build_plan_steps(
        intent=intent_result,
        answer_type="data_analysis",
        execution_mode=execution_mode,
        scope=scope,
        dataset_name=dataset["name"],
        chart_type=chart_type,
    )
    payload = {
        "session_id": session_id,
        "message": "分析完成。" + (" 已继承上轮对话条件。" if context_applied else ""),
        "intent": intent,
        "intent_label": intent_result.label,
        "intent_confidence": intent_result.confidence,
        "intent_method": intent_result.method,
        "intent_reason": intent_result.reason,
        "sql_source": plan_source,
        "plan": plan_titles(plan_steps),
        "plan_steps": plan_steps,
        "sql": safe_sql,
        "columns": list(rows[0].keys()) if rows else [query_plan.x_field, query_plan.y_field],
        "rows": rows,
        "chart": chart,
        "chart_sections": chart_sections,
        "omitted_chart_sections": omitted_chart_sections,
        "insights": insights,
        "knowledge_refs": knowledge,
        "execution_mode": execution_mode,
        "analysis_engine": analysis_engine,
        "sql_repair": sql_repair,
        "technical_notes": technical_notes,
        "python_analysis": python_analysis,
        "python_code": python_code,
        "field_mappings": query_plan.field_mappings,
        "answer_type": "data_analysis",
        "context_applied": context_applied,
        "effective_question": effective_question,
        "reflection_applied": reflection_applied,
        "narrative_applied": narrative_applied,
    }
    _store_assistant(session_id, payload, "analyze", dataset_id)
    yield {"type": "result", "data": payload}
    yield {"type": "done"}


def _build_stream_plan_titles(intent_result, answer_type: str) -> list[str]:
    """Generate step titles for the streaming plan event (before full plan_steps are built)."""
    if answer_type == "knowledge_qa":
        return ["识别意图", "检索知识库", "生成回答"]
    if intent_result.label == "anomaly_attribution":
        return ["识别意图", "分析数据集", "构建查询", "异常归因分析"]
    return ["识别意图", "分析数据集", "构建查询", "生成洞察"]
