from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx

from ..config import get_settings


IntentLabel = Literal[
    "data_query",
    "trend_analysis",
    "anomaly_attribution",
    "knowledge_qa",
    "report_generation",
]


INTENT_DISPLAY_NAMES: dict[IntentLabel, str] = {
    "data_query": "指标查询",
    "trend_analysis": "趋势分析",
    "anomaly_attribution": "异常归因",
    "knowledge_qa": "知识库问答",
    "report_generation": "报告生成",
}


@dataclass
class IntentResult:
    label: IntentLabel
    confidence: float
    method: str
    reason: str = ""

    @property
    def display_name(self) -> str:
        return INTENT_DISPLAY_NAMES[self.label]

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["display_name"] = self.display_name
        return payload


FEW_SHOT_EXAMPLES: list[dict[str, str]] = [
    {"question": "查询各地区销售额", "label": "data_query"},
    {"question": "统计本月各渠道订单数", "label": "data_query"},
    {"question": "目前数据库是关于什么的", "label": "data_query"},
    {"question": "当前数据源是什么内容", "label": "data_query"},
    {"question": "介绍一下这张表有哪些字段", "label": "data_query"},
    {"question": "分析近30天销售额走势", "label": "trend_analysis"},
    {"question": "按月份看利润趋势", "label": "trend_analysis"},
    {"question": "为什么华东销售额突然下降", "label": "anomaly_attribution"},
    {"question": "分析投诉率异常升高的原因", "label": "anomaly_attribution"},
    {"question": "销售额的计算口径是什么", "label": "knowledge_qa"},
    {"question": "转化率怎么计算", "label": "knowledge_qa"},
    {"question": "把本次分析生成 Word 报告", "label": "report_generation"},
    {"question": "导出 PDF 分析报告", "label": "report_generation"},
]


def _clip_confidence(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 4)


def _is_dataset_meta_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.lower())
    data_object_terms = (
        "数据库",
        "数据源",
        "数据集",
        "数据表",
        "业务库",
        "当前库",
        "当前表",
        "当前数据",
        "这个库",
        "这个表",
        "这张表",
        "表结构",
        "字段",
        "schema",
        "database",
        "dataset",
        "table",
    )
    meta_question_terms = (
        "关于什么",
        "是什么内容",
        "什么内容",
        "有什么内容",
        "有哪些内容",
        "有哪些字段",
        "字段含义",
        "字段说明",
        "表结构",
        "数据概览",
        "整体概览",
        "介绍一下",
        "描述一下",
        "概览一下",
        "profile",
        "overview",
        "describe",
    )
    return any(term.lower() in compact for term in data_object_terms) and any(
        term.lower() in compact for term in meta_question_terms
    )


def classify_intent_rules(question: str, history: list[dict[str, Any]] | None = None) -> IntentResult:
    text = question.strip()
    compact = re.sub(r"\s+", "", text.lower())
    history = history or []

    report_terms = ("报告", "导出", "生成文档", "word", "pdf", "docx", "markdown", "下载")
    anomaly_terms = ("为什么", "原因", "归因", "异常", "波动", "突增", "骤增", "暴增", "骤降", "下滑", "下降", "降低", "偏高", "偏低", "出了问题")
    knowledge_terms = ("什么是", "是什么", "如何计算", "怎么计算", "怎样计算", "口径", "定义", "含义", "业务规则", "字段说明", "知识库", "是否计入")
    trend_terms = ("趋势", "走势", "变化", "按月", "月度", "月份", "每日", "每天", "按天", "近", "最近", "同比", "环比")
    data_terms = ("查询", "统计", "分析", "计算", "汇总", "排行", "排名", "最高", "最低", "多少", "占比", "分布", "对比")

    if any(term.lower() in compact for term in report_terms):
        return IntentResult("report_generation", 0.95, "rules", "命中报告/导出类词汇")

    if _is_dataset_meta_question(text):
        return IntentResult("data_query", 0.96, "rules", "用户询问当前数据库/数据源/数据表概览")

    if any(term in text for term in anomaly_terms):
        return IntentResult("anomaly_attribution", 0.92, "rules", "命中异常、原因或归因词汇")

    if any(term in text for term in knowledge_terms):
        data_conflict = any(term in text for term in ("统计", "趋势", "排行", "最高", "最低", "图表", "多少"))
        if not data_conflict:
            return IntentResult("knowledge_qa", 0.94, "rules", "命中指标口径/定义类词汇")

    if any(term in text for term in trend_terms) or re.search(r"(近|最近)\s*\d+\s*(天|日|周|个月|月)", text):
        return IntentResult("trend_analysis", 0.91, "rules", "命中趋势或时间序列词汇")

    if any(term in text for term in data_terms):
        return IntentResult("data_query", 0.88, "rules", "命中查询/统计类词汇")

    previous_payload = next(
        (
            item.get("payload")
            for item in reversed(history)
            if item.get("role") == "assistant" and isinstance(item.get("payload"), dict)
        ),
        None,
    )
    if previous_payload and str(text) in {"继续", "再看", "这个呢", "为什么", "解释一下"}:
        previous_label = previous_payload.get("intent_label")
        if previous_label in INTENT_DISPLAY_NAMES:
            return IntentResult(previous_label, 0.7, "rules", "短追问继承上一轮意图")

    return IntentResult("data_query", 0.62, "rules", "未命中强特征，默认按指标查询处理")


def _parse_llm_intent(content: str) -> IntentResult:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
    data = json.loads(text)
    label = str(data.get("label", "")).strip()
    if label not in INTENT_DISPLAY_NAMES:
        raise ValueError("LLM 返回了未知意图")
    confidence = _clip_confidence(float(data.get("confidence", 0.0)))
    reason = str(data.get("reason", ""))[:200]
    return IntentResult(label, confidence, "llm", reason)


async def classify_intent(question: str, history: list[dict[str, Any]] | None = None) -> IntentResult:
    settings = get_settings()
    fallback = classify_intent_rules(question, history)
    if _is_dataset_meta_question(question):
        return fallback
    if not settings.llm_configured:
        return fallback

    examples = "\n".join(f"- {item['question']} => {item['label']}" for item in FEW_SHOT_EXAMPLES)
    recent_history = [
        {"role": item.get("role"), "content": str(item.get("content", ""))[:300]}
        for item in (history or [])[-4:]
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "你是企业数据智能体的意图分类器。只能从以下 5 类中选择一个："
                "data_query, trend_analysis, anomaly_attribution, knowledge_qa, report_generation。"
                "重要规则：用户询问当前数据库、当前数据源、数据集、数据表是关于什么、是什么内容、有哪些字段、表结构或数据概览时，必须归类为 data_query，不能归类为 knowledge_qa。"
                "返回严格 JSON：{\"label\":\"...\",\"confidence\":0.0-1.0,\"reason\":\"...\"}。"
                "temperature=0，优先判断用户当前请求本身；必要时参考历史上下文。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "few_shot_examples": examples,
                    "history": recent_history,
                    "question": question,
                    "fallback_hint": fallback.label,
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={"model": settings.llm_model, "temperature": 0, "messages": messages},
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            result = _parse_llm_intent(content)
            if result.confidence < 0.55:
                return fallback
            return result
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return fallback
