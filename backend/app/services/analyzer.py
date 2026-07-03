from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..config import get_settings
from ..db import connect, using_mysql
from .intent_classifier import IntentResult, classify_intent
from .chart_recommender import recommend_chart
from .knowledge import search_knowledge
from .llm import answer_from_knowledge, patch_insights, polish_insights, reflect_on_insights
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
from .sql_generator import FieldNotFoundError, generate_llm_sql, query_plan_source, repair_llm_sql
from .field_aliases import field_mapping_notes, find_column_by_alias, resolve_field_references


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
                "SELECT name, data_type, description FROM dataset_columns WHERE dataset_id = ? ORDER BY id",
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
        if any(keyword.lower() in haystack for keyword in keywords):
            return column["name"]
    return None


def _column_profile(columns: list[dict]) -> dict[str, str | None]:
    numeric_types = ("int", "float", "double", "decimal", "real", "number")
    first_numeric = next(
        (item["name"] for item in columns if any(token in item["data_type"].lower() for token in numeric_types)),
        None,
    )
    first_dimension = next(
        (item["name"] for item in columns if not any(token in item["data_type"].lower() for token in numeric_types)),
        columns[0]["name"] if columns else None,
    )
    influencer_dimension = find_column_by_alias(columns, "达人", numeric=False) or find_column_by_alias(columns, "达人")
    customer_dimension = find_column_by_alias(columns, "客户", numeric=False) or _find_column(
        columns, ("customer", "client", "user", "member", "buyer", "客户", "用户", "会员", "买家"), False
    )
    return {
        "date": _find_column(columns, ("date", "time", "日期", "时间")),
        "region": _find_column(columns, ("region", "area", "地区", "区域")),
        "product": _find_column(columns, ("product", "category", "产品", "品类", "类别")),
        "channel": _find_column(columns, ("channel", "渠道")),
        "customer": customer_dimension,
        "influencer": influencer_dimension,
        "sales": (
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
            or first_numeric
        ),
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
    dynamic_metric = resolve_field_references(question, columns, numeric=True, limit=1, min_score=0.72)
    if dynamic_metric:
        match = dynamic_metric[0]
        label = str(match.get("label") or match["column"])
        return (f"ROUND(SUM({match['column']}), 2)", label, "", dynamic_metric)
    selected = profile["sales"] or profile["first_numeric"]
    if not selected:
        raise ValueError("当前数据集没有可聚合的数值字段")
    label = "销售额" if profile["sales"] == selected else str(selected)
    return (f"ROUND(SUM({selected}), 2)", label, "元" if label == "销售额" else "", [])


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
        (("产品", "展品", "品类", "类别"), profile["product"], "产品类别"),
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
        label = str(match.get("label") or column)
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
        for row in rows:
            totals[_series_label(row, series_fields)] += float(row[y_field] or 0)
        ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        peak = max(rows, key=lambda row: float(row[y_field] or 0))
        peak_series = _series_label(peak, series_fields)

        # --- 基础排名洞察 ---
        insights = [
            f"{ranked[0][0]}累计{y_field}最高，为 {ranked[0][1]:,.2f}。",
            f"{ranked[-1][0]}累计{y_field}最低，为 {ranked[-1][1]:,.2f}。",
            f"单点峰值出现在 {peak[x_field]}，{peak_series}的{y_field}为 {float(peak[y_field] or 0):,.2f}。",
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

    numeric = [(row[x_field], float(row[y_field] or 0)) for row in rows]
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


def _floatable(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


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
        primary_metric = profile.get("sales") or profile.get("first_numeric")
        primary_metric_label = "销售额/GMV" if profile.get("sales") else str(primary_metric or "指标值")

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

    if not wants_database_context(effective_question):
        document_result = await analyze_uploaded_document(
            session_id=session_id,
            filename=filename,
            text=doc_text,
            question=effective_question,
            history=history,
        )
        document_result["document_analyzed"] = doc_title
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

    # Step 1: Intent classification
    intent_result = await classify_intent(effective_question, history)
    if _is_knowledge_question(question, history) and intent_result.label == "data_query":
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

    fallback_message = sql_repair.get("field_not_found_fallback") if isinstance(sql_repair, dict) else None
    if fallback_message:
        fallback_note = (
            "字段口径说明：模型没有在数据表中精确找到用户提到的字段，"
            f"已先按当前可用字段「{query_plan.x_field} / {query_plan.y_field}」生成近似分析；"
            "如果口径不符合预期，可以继续指定字段名或补充业务说明。"
        )
        insights = [fallback_note] + [item for item in insights if item != fallback_note]

    mapping_notes = field_mapping_notes(query_plan.field_mappings)
    if mapping_notes:
        insights = mapping_notes + [item for item in insights if item not in mapping_notes]

    chart_sections, omitted_chart_sections = _build_data_chart_sections(
        question=effective_question,
        intent_label=intent_result.label,
        dataset=dataset,
        rows=rows,
        chart=chart,
        query_plan=query_plan,
        limit=settings.query_row_limit,
    )
    if omitted_chart_sections:
        insights.append(
            "图表数量限制：本次最多展示 "
            f"{MAX_CHART_SECTIONS} 张图，已省略：{'、'.join(omitted_chart_sections[:8])}"
            + ("等。" if len(omitted_chart_sections) > 8 else "。")
            + "你可以继续追问其中某个维度单独展开。"
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
        "python_analysis": python_analysis,
        "python_code": python_code,
        "field_mappings": query_plan.field_mappings,
        "answer_type": "data_analysis",
        "context_applied": context_applied,
        "effective_question": effective_question,
        "reflection_applied": reflection_applied,
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
