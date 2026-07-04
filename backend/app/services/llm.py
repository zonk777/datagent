from __future__ import annotations

import json
import re

import httpx

from ..config import get_settings


def _parse_json_object(content: str) -> dict:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM output is not a JSON object")
    return parsed


async def answer_from_knowledge(
    question: str,
    knowledge: list[dict],
    history: list[dict] | None = None,
) -> str:
    """Answer from retrieved business knowledge with flexible partial reasoning.

    Q-005: When knowledge is incomplete, the LLM is allowed to distinguish
    known parts from missing parts and offer constructive guidance,
    rather than a blunt response. Temperature raised from 0.1 to 0.3.
    """
    if not knowledge:
        return (
            "知识库中暂时没有找到足够相关的内容。"
            "建议：1) 补充指标口径说明；2) 上传业务规则文档；3) 添加数据字典。"
            "你也可以尝试用更具体的关键词重新提问。"
        )

    settings = get_settings()
    if not settings.llm_configured:
        return "\n".join(f"{item['title']}：{item['content']}" for item in knowledge[:3])

    recent_history = []
    for item in (history or [])[-6:]:
        role = item.get("role")
        if role in ("user", "assistant") and item.get("content"):
            recent_history.append({"role": role, "content": str(item["content"])[:1200]})
    context = [
        {
            "title": item.get("title", ""),
            "content": item.get("content", ""),
            "category": item.get("category", ""),
        }
        for item in knowledge[:5]
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "你是企业业务知识助手。请根据提供的知识片段回答用户问题。\n"
                "规则：\n"
                "1. 优先引用知识片段中的内容，不得凭空编造制度、指标或数字。\n"
                "2. 当知识与问题部分匹配时，主动区分「已知部分」和「缺失部分」：\n"
                "   - 已知部分：基于现有知识可以回答的内容\n"
                "   - 缺失部分：明确说明还需要什么信息（如「本月实际数据」「具体阈值标准」）\n"
                "   - 建议：给出可操作的建议（如「请提供本月销售数据后我可以帮您对比目标」）\n"
                "3. 回答应简洁有条理，末尾用「依据：」列出引用的知识标题。\n"
                "4. 即使知识不完整，也优先给出部分有用的信息，而不是简单说「知识不足」。"
            ),
        },
        *recent_history,
        {
            "role": "user",
            "content": json.dumps({"question": question, "knowledge": context}, ensure_ascii=False),
        },
    ]
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={"model": settings.llm_model, "temperature": 0.3, "messages": messages},
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"].strip()
            if answer:
                return answer
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        pass
    return "\n".join(f"{item['title']}：{item['content']}" for item in knowledge[:3])


async def polish_insights(
    question: str,
    rows: list[dict],
    draft: list[str],
    knowledge: list[dict],
    intent_reason: str = "",
    plan_source: str = "",
) -> list[str]:
    """Use LLM to refine deterministic draft insights into natural-language takeaways.

    Q-006: temperature raised from 0.2 to 0.5 to allow more creative phrasing;
    system prompt expanded to encourage trend detection, anomaly interpretation,
    and actionable recommendations beyond pure restatement of numbers.

    Also injects intent_reason and plan_source context when available.
    """
    settings = get_settings()
    if not settings.llm_configured:
        return draft

    system_extra = ""
    if intent_reason:
        system_extra += f" 本次分析意图: {intent_reason}。"
    if plan_source:
        system_extra += f" SQL 由 {plan_source} 生成，请据此判断数据口径可信度。"

    payload = {
        "model": settings.llm_model,
        "temperature": 0.5,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是企业数据分析师。请基于提供的聚合数据与 draft 草稿，提炼 2~4 条精炼的业务洞察。\n"
                    "要求：\n"
                    "1. 不要简单复述数字，要提炼出趋势方向、异常信号或可操作建议。\n"
                    "2. 如有业务知识片段，将数据与业务口径结合起来解读。\n"
                    "3. 如果数据中存在明显的上升/下降趋势、异常值、集中度问题，请明确指出。\n"
                    "4. 每条洞察应有「结论 + 数据支撑」结构。\n"
                    "5. 输出为 JSON 字符串数组，最多 4 条。\n"
                    "6. 不虚构数据中不存在的事实，不确定的推断用「可能」「建议关注」等措辞。"
                    + system_extra
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "aggregate_rows": rows[:30],
                        "draft": draft,
                        "business_knowledge": knowledge,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                return parsed[:4]
    except (httpx.HTTPError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return draft


async def narrate_database_insights(
    *,
    question: str,
    dataset_name: str,
    rows: list[dict],
    draft: list[str],
    chart_sections: list[dict],
    knowledge: list[dict],
    omitted_chart_sections: list[str] | None = None,
    technical_notes: list[str] | None = None,
) -> dict:
    """Turn database query output into advisor-style business explanation.

    This is intentionally separate from SQL generation.  It receives already
    computed rows/sections and only rewrites the explanation layer, so internal
    mechanics such as SQL aliases or repaired column names never leak to users.
    """
    settings = get_settings()
    if not settings.llm_configured:
        return {}

    section_payload = []
    for section in (chart_sections or [])[:8]:
        chart = section.get("chart") or {}
        section_payload.append(
            {
                "id": section.get("id"),
                "title": section.get("title"),
                "description": section.get("description"),
                "chart_type": chart.get("type"),
                "x_field": chart.get("x_field"),
                "y_field": chart.get("y_field"),
                "rows": (section.get("rows") or [])[:12],
                "draft_insights": (section.get("insights") or [])[:4],
            }
        )

    payload = {
        "model": settings.llm_model,
        "temperature": 0.45,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是企业数据智能顾问。你已经拿到了数据库查询和图表所需的数据，"
                    "现在只负责把结果解释成自然、有判断、有原因的业务分析。\n"
                    "硬性要求：\n"
                    "1. 不要暴露内部实现细节：禁止出现 SQL、字段别名修正、column_、返回列、修复、LLM、query、"
                    "字段映射、实际列、期望列、template、repair 等工程词。\n"
                    "2. 不要机械复述“最高/最低/极差比”，必须说明这意味着什么、可能原因是什么、下一步该验证什么。\n"
                    "3. 每条结论都要有「结论 + 数据支撑 + 业务含义/原因解释」；原因不确定时用“可能”“需要结合业务验证”。\n"
                    "4. 对每个图表章节生成 1~3 条解释，解释要贴合该图表，不要把所有结论堆到总体建议。\n"
                    "5. 总体结论给 2~4 条，最后给 2~4 个后续追问建议。\n"
                    "6. 不能编造输入数据中没有的数值或对象。\n"
                    "7. 输出严格 JSON："
                    '{"overall":["..."],"sections":[{"id":"章节id","insights":["..."]}],'
                    '"followups":["..."]}'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "dataset_name": dataset_name,
                        "rows": rows[:30],
                        "draft": draft[:8],
                        "chart_sections": section_payload,
                        "business_knowledge": (knowledge or [])[:5],
                        "omitted_chart_sections": omitted_chart_sections or [],
                        "internal_notes_do_not_show": technical_notes or [],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            parsed = _parse_json_object(content)
            overall = [str(item).strip() for item in parsed.get("overall", []) if str(item).strip()]
            followups = [str(item).strip() for item in parsed.get("followups", []) if str(item).strip()]
            sections = []
            for item in parsed.get("sections", []):
                if not isinstance(item, dict):
                    continue
                section_id = str(item.get("id") or "").strip()
                insights = [str(text).strip() for text in item.get("insights", []) if str(text).strip()]
                if section_id and insights:
                    sections.append({"id": section_id, "insights": insights[:3]})
            return {
                "overall": overall[:4],
                "sections": sections,
                "followups": followups[:4],
            }
    except (httpx.HTTPError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# A-003 / A-005: Anchored Lightweight Reflection
# ---------------------------------------------------------------------------
# Three safety mechanisms:
#   1. Immutable anchors -- 3 must-answer points extracted from the question.
#      The reflector can ONLY check these, nothing else.
#   2. Hard limit -- 1 round reflection + 1 round fix. If not passing, keep
#      the original output.
#   3. Delta fix -- the reflector outputs only "what's missing" (a patch);
#      the patch is APPENDED to the original insights, never rewrites them.
# ---------------------------------------------------------------------------


async def reflect_on_insights(
    question: str,
    anchors: list[str],
    insights: list[str],
    rows: list[dict],
    knowledge: list[dict],
) -> dict:
    """Check whether current insights cover all immutable anchors.

    Returns a dict:
        {
            "has_gaps": bool,
            "anchors_missing": list[str],
            "gap_description": str,
        }

    On LLM failure or timeout, returns {"has_gaps": False} -- graceful
    degradation: the original insights pass through unchanged.
    """
    settings = get_settings()
    if not settings.llm_configured or not anchors:
        return {"has_gaps": False}

    payload = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是数据分析质量检查器。你只有一个任务：检查当前洞察是否覆盖了所有「锚点」。\n"
                    "规则：\n"
                    "1. 只检查下面列出的锚点，不要自行添加任何其他检查项。\n"
                    "2. 每个锚点只需判断「已覆盖」或「未覆盖」。\n"
                    "3. 覆盖的标准：洞察中是否明确提到了该锚点对应的信息维度。\n"
                    "4. 输出严格 JSON：\n"
                    '   {"anchors_missing": ["锚点1", ...], "gap_description": "缺失说明"}\n'
                    "5. 如果所有锚点都已覆盖，anchors_missing 为空数组，gap_description 为空字符串。\n"
                    "6. 不要输出任何 JSON 之外的内容。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "anchors": anchors,
                        "insights": insights,
                        "sample_data": rows[:5],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            parsed = json.loads(content)
            missing = list(parsed.get("anchors_missing", []))
            return {
                "has_gaps": len(missing) > 0,
                "anchors_missing": missing,
                "gap_description": str(parsed.get("gap_description", "")),
            }
    except (httpx.HTTPError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return {"has_gaps": False}


async def patch_insights(
    question: str,
    anchors_missing: list[str],
    gap_description: str,
    insights: list[str],
    rows: list[dict],
    knowledge: list[dict],
) -> list[str]:
    """Generate supplementary insights that fill specific gaps.

    IMPORTANT: The output is a PATCH -- it only fills what's missing.
    The caller APPENDS this to the original insights; nothing is rewritten.

    Returns a list of 1-2 insight strings, or [] on failure.
    """
    settings = get_settings()
    if not settings.llm_configured or not anchors_missing:
        return []

    payload = {
        "model": settings.llm_model,
        "temperature": 0.4,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是数据分析补充器。已有洞察缺少了一些信息维度，你需要补充缺失的内容。\n"
                    "规则：\n"
                    "1. 只补充「缺失锚点」中列出的内容，不要改写已有洞察。\n"
                    "2. 基于提供的数据得出补充结论，不虚构数据。\n"
                    "3. 如果有业务知识片段，可结合解读。\n"
                    "4. 输出严格 JSON 字符串数组，最多 2 条。\n"
                    "5. 如果确实无法从数据中得出缺失锚点的结论，输出空数组 []。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "anchors_missing": anchors_missing,
                        "gap_description": gap_description,
                        "existing_insights": insights,
                        "data": rows[:20],
                        "business_knowledge": (knowledge or [])[:3],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            parsed = json.loads(content)
            if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                return [item for item in parsed if item.strip()][:2]
    except (httpx.HTTPError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return []
