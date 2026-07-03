from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


FIELD_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "日期": ("日期", "时间", "date", "time", "day", "month", "year", "created_at", "updated_at"),
    "地区": ("地区", "区域", "大区", "省份", "城市", "region", "area", "province", "city", "district"),
    "产品": ("产品", "商品", "品类", "类别", "展品", "product", "sku", "spu", "item", "category", "goods"),
    "渠道": ("渠道", "来源", "入口", "channel", "source", "medium", "traffic_source"),
    "客户": ("客户", "用户", "会员", "买家", "消费者", "customer", "client", "user", "member", "buyer", "consumer"),
    "门店": ("门店", "店铺", "网点", "门市", "store", "shop", "outlet", "branch"),
    "平台": ("平台", "站点", "platform", "site", "marketplace"),
    "品牌": ("品牌", "brand"),
    "供应商": ("供应商", "供货商", "厂商", "supplier", "vendor", "provider"),
    "员工": ("员工", "销售员", "业务员", "顾问", "employee", "staff", "salesperson", "sales_rep", "consultant"),
    "达人": (
        "达人",
        "达人名称",
        "达人昵称",
        "达人账号",
        "达人id",
        "带货达人",
        "主播",
        "主播名称",
        "主播id",
        "kol",
        "koc",
        "influencer",
        "influencer_name",
        "creator",
        "creator_name",
        "talent",
        "talent_name",
        "blogger",
        "博主",
        "网红",
        "创作者",
        "作者",
        "专家",
    ),
    "销售额": (
        "销售额",
        "营收",
        "收入",
        "成交金额",
        "金额",
        "GMV",
        "gmv",
        "销售净额",
        "实付金额",
        "支付金额",
        "sales",
        "sales_amount",
        "revenue",
        "paid_amount",
        "net_paid",
        "item_gmv",
        "item_paid_amount",
        "item_net_paid_amount",
        "amount",
    ),
    "订单数": ("订单数", "订单量", "销量", "成交单", "orders", "order_count", "order_num", "sales_count"),
    "利润": ("利润", "毛利", "净利", "profit", "gross_profit", "net_profit"),
    "成本": ("成本", "花费", "费用", "cost", "expense", "spend"),
    "退款": ("退款", "退货", "refund", "return"),
    "访问量": ("访问量", "访客", "流量", "浏览", "pv", "uv", "visit", "visits", "traffic", "view", "views"),
    "转化": ("转化", "成交人数", "转化人数", "conversion", "conversions", "converted"),
    "粉丝数": ("粉丝", "粉丝数", "关注数", "fans", "followers", "follower_count"),
    "点赞数": ("点赞", "点赞数", "like", "likes", "like_count"),
    "评论数": ("评论", "评论数", "comment", "comments", "comment_count"),
    "点击数": ("点击", "点击数", "click", "clicks", "click_count"),
    "客户集中度": (
        "客户集中度",
        "客户集中",
        "大客户",
        "前五大客户",
        "前十大客户",
        "top客户",
        "top customer",
        "top_customers",
        "customer_concentration",
        "concentration",
        "top_client",
        "key_account",
    ),
    "应收账款": (
        "应收账款",
        "应收",
        "账款",
        "赊销",
        "回款",
        "欠款",
        "accounts_receivable",
        "account_receivable",
        "receivable",
        "receivables",
        "ar_amount",
        "ar_balance",
    ),
    "账龄": (
        "账龄",
        "逾期天数",
        "逾期",
        "账期",
        "账龄段",
        "aging",
        "ageing",
        "age_bucket",
        "ar_age",
        "receivable_age",
        "overdue_days",
        "days_overdue",
    ),
    "现金流": (
        "现金流",
        "经营现金流",
        "现金流量",
        "净现金流",
        "cash_flow",
        "operating_cash_flow",
        "net_cash_flow",
        "cfo",
    ),
    "资产": ("资产", "总资产", "流动资产", "固定资产", "asset", "assets", "total_assets"),
    "负债": ("负债", "总负债", "债务", "liability", "liabilities", "debt", "total_liabilities"),
    "研发投入": (
        "研发投入",
        "研发费用",
        "研发",
        "研发支出",
        "rd",
        "r_d",
        "research",
        "development",
        "r_and_d",
    ),
}

FIELD_STOPWORDS = {
    "分析",
    "统计",
    "查询",
    "展示",
    "生成",
    "报告",
    "数据",
    "字段",
    "维度",
    "指标",
    "结果",
    "图表",
    "情况",
    "一下",
    "看看",
    "按照",
    "根据",
    "每个",
    "各个",
    "各类",
    "全部",
    "完整",
    "多维度",
    "最近",
    "近",
    "本月",
    "今年",
}

NUMERIC_TYPES = ("int", "float", "double", "decimal", "real", "number")


def normalize_alias_text(value: Any) -> str:
    text = str(value or "").lower()
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def alias_terms(group: str) -> tuple[str, ...]:
    return FIELD_ALIAS_GROUPS.get(group, (group,))


def text_mentions_alias(text: str, group: str) -> bool:
    normalized = normalize_alias_text(text)
    return any(normalize_alias_text(term) in normalized for term in alias_terms(group))


def column_matches_alias(column: dict[str, Any], group: str) -> bool:
    haystack = normalize_alias_text(f"{column.get('name', '')} {column.get('description', '')}")
    return any(normalize_alias_text(term) in haystack for term in alias_terms(group))


def is_numeric_column(column: dict[str, Any]) -> bool:
    return any(token in str(column.get("data_type", "")).lower() for token in NUMERIC_TYPES)


def find_column_by_alias(
    columns: list[dict[str, Any]],
    group: str,
    *,
    numeric: bool | None = None,
) -> str | None:
    for column in columns:
        is_numeric = is_numeric_column(column)
        if numeric is not None and is_numeric != numeric:
            continue
        if column_matches_alias(column, group):
            return str(column.get("name") or "")
    return None


def _split_identifier(value: Any) -> list[str]:
    raw = str(value or "")
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
    parts = re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", raw.lower())
    return [part for part in parts if part and part not in FIELD_STOPWORDS]


def _question_terms(question: str) -> list[str]:
    text = str(question or "")
    chunks = re.findall(r"[0-9A-Za-z_\-\u4e00-\u9fff]+", text)
    terms: list[str] = []
    for group, aliases in FIELD_ALIAS_GROUPS.items():
        if text_mentions_alias(text, group):
            exact_aliases = [alias for alias in aliases if normalize_alias_text(alias) in normalize_alias_text(text)]
            terms.extend(sorted(exact_aliases, key=lambda item: len(normalize_alias_text(item)), reverse=True))
            terms.append(group)
    for chunk in chunks:
        for part in _split_identifier(chunk):
            cleaned = part.strip()
            if not cleaned or cleaned in FIELD_STOPWORDS:
                continue
            if cleaned.isdigit():
                continue
            if re.fullmatch(r"\d+(天|日|周|月|个月|年)?", cleaned):
                continue
            terms.append(cleaned)
            if re.search(r"[\u4e00-\u9fff]", cleaned) and 3 <= len(cleaned) <= 12:
                for size in range(2, min(6, len(cleaned)) + 1):
                    for start in range(0, len(cleaned) - size + 1):
                        token = cleaned[start:start + size]
                        if token not in FIELD_STOPWORDS:
                            terms.append(token)
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        normalized = normalize_alias_text(term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(term)
    return result[:80]


def _char_bigrams(text: str) -> set[str]:
    normalized = normalize_alias_text(text)
    if len(normalized) <= 1:
        return {normalized} if normalized else set()
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _column_text(column: dict[str, Any]) -> str:
    return f"{column.get('name', '')} {column.get('description', '')}"


def _score_term_for_column(term: str, column: dict[str, Any]) -> tuple[float, str]:
    norm_term = normalize_alias_text(term)
    norm_name = normalize_alias_text(column.get("name", ""))
    norm_desc = normalize_alias_text(column.get("description", ""))
    haystack = normalize_alias_text(_column_text(column))
    if not norm_term or not haystack:
        return 0.0, "empty"

    for group, aliases in FIELD_ALIAS_GROUPS.items():
        term_in_group = any(normalize_alias_text(alias) == norm_term for alias in (group, *aliases))
        if term_in_group and column_matches_alias(column, group):
            return 0.96, f"业务同义词：{group}"

    if norm_term == norm_name or norm_term == norm_desc:
        return 0.98, "字段名/说明完全匹配"
    if norm_term in norm_name or norm_term in norm_desc:
        return 0.9 if len(norm_term) >= 2 else 0.55, "字段名/说明包含该词"
    if norm_name and norm_name in norm_term:
        return 0.82, "用户表述包含字段名"
    if norm_desc and norm_desc in norm_term:
        return 0.82, "用户表述包含字段说明"

    term_tokens = set(_split_identifier(term))
    column_tokens = set(_split_identifier(_column_text(column)))
    overlap = _jaccard(term_tokens, column_tokens)
    if overlap:
        return min(0.82, 0.45 + overlap * 0.42), "英文/分词重合"

    seq = max(
        SequenceMatcher(None, norm_term, norm_name).ratio() if norm_name else 0.0,
        SequenceMatcher(None, norm_term, norm_desc).ratio() if norm_desc else 0.0,
    )
    bigram = max(_jaccard(_char_bigrams(norm_term), _char_bigrams(norm_name)), _jaccard(_char_bigrams(norm_term), _char_bigrams(norm_desc)))
    score = max(seq * 0.72, bigram * 0.84)
    return score, "名称相似度匹配"


def resolve_field_references(
    question: str,
    columns: list[dict[str, Any]],
    *,
    numeric: bool | None = None,
    limit: int = 3,
    min_score: float = 0.66,
) -> list[dict[str, Any]]:
    """Resolve user-friendly field names to concrete dataset columns.

    The resolver is intentionally hybrid: exact/alias matches are preferred,
    then it falls back to fuzzy similarity.  Fuzzy matches are returned with
    lower confidence so callers can explain the assumption to the user.
    """
    terms = _question_terms(question)
    matches: dict[str, dict[str, Any]] = {}
    for column in columns:
        if numeric is not None and is_numeric_column(column) != numeric:
            continue
        best_score = 0.0
        best_term = ""
        best_reason = ""
        for term in terms:
            score, reason = _score_term_for_column(term, column)
            if score > best_score:
                best_score = score
                best_term = term
                best_reason = reason
        if best_score < min_score:
            continue
        column_name = str(column.get("name") or "")
        confidence = "high" if best_score >= 0.86 else ("medium" if best_score >= 0.72 else "low")
        label = best_term or column.get("description") or column_name
        matches[column_name] = {
            "term": best_term,
            "column": column_name,
            "description": column.get("description", ""),
            "data_type": column.get("data_type", ""),
            "label": str(label).strip() or column_name,
            "score": round(best_score, 3),
            "confidence": confidence,
            "reason": best_reason,
            "numeric": is_numeric_column(column),
        }
    return sorted(matches.values(), key=lambda item: item["score"], reverse=True)[:limit]


def field_mapping_notes(matches: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        term = str(match.get("term") or "").strip()
        column = str(match.get("column") or "").strip()
        if not term or not column or (term, column) in seen:
            continue
        seen.add((term, column))
        confidence = match.get("confidence")
        if confidence == "high":
            notes.append(f"字段理解：你提到的“{term}”已按数据表字段「{column}」处理。")
        else:
            notes.append(f"字段理解：你提到的“{term}”系统暂按字段「{column}」处理；如果口径不准确，可在数据源字段说明或业务知识库中补充更明确的映射。")
    return notes[:3]


def alias_hints_for_question(question: str, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for group, terms in FIELD_ALIAS_GROUPS.items():
        if not text_mentions_alias(question, group):
            continue
        matched_columns = [
            {
                "name": column.get("name", ""),
                "description": column.get("description", ""),
                "data_type": column.get("data_type", ""),
            }
            for column in columns
            if column_matches_alias(column, group)
        ]
        hints.append(
            {
                "business_term": group,
                "aliases": list(terms),
                "matched_columns": matched_columns,
                "instruction": f"用户说“{group}”时，优先理解为 matched_columns 中的业务对象维度字段。",
            }
        )
    fuzzy_hints = resolve_field_references(question, columns, limit=5, min_score=0.72)
    for match in fuzzy_hints:
        if any(match["column"] in {item.get("name") for item in hint.get("matched_columns", [])} for hint in hints):
            continue
        hints.append(
            {
                "business_term": match["term"],
                "aliases": [],
                "matched_columns": [{
                    "name": match["column"],
                    "description": match["description"],
                    "data_type": match["data_type"],
                }],
                "confidence": match["confidence"],
                "score": match["score"],
                "instruction": (
                    f"用户说“{match['term']}”时，可暂按字段 {match['column']} 理解；"
                    "如果 confidence 不是 high，请在回答中说明这是近似字段匹配。"
                ),
            }
        )
    return hints
