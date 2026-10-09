"""LLM-based semantic extraction via an OpenAI-compatible endpoint.

Config (env): OPENAI_API_KEY (required), OPENAI_BASE_URL (optional),
OPENAI_MODEL (default gpt-4o-mini). No keys are hardcoded.
Every call's token usage is returned so callers can persist cost data.
"""
import json
import logging
import os

import requests

log = logging.getLogger(__name__)

API_KEY = os.environ.get("OPENAI_API_KEY")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# USD per 1M tokens, for cost reporting only
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

SYSTEM_PROMPT = """你是需求信号分析器。从用户给出的英文/中文文本中抽取语义字段，输出 JSON。
要求：
- 每个非空字段必须给出 evidence：原文中的逐字引用片段（英文保持原文，不要翻译）。
- 无法从原文找到依据的字段，值设为空字符串 ""，evidence 也设为空字符串。禁止编造。
- urgency 只能取 "高"/"中"/"低"/""。

输出 JSON 结构（不要输出任何其他文字）：
{"pain_point": {"value": "...", "evidence": "..."},
 "audience": {"value": "...", "evidence": "..."},
 "scenario": {"value": "...", "evidence": "..."},
 "urgency": {"value": "...", "evidence": "..."},
 "current_solution": {"value": "...", "evidence": "..."},
 "alternatives": {"value": "...", "evidence": "..."},
 "supply_gap": {"value": "...", "evidence": "..."}}"""

USER_TEMPLATE = """来源类型: {signal_type}
标题: {title}
正文: {description}"""


class LLMNotConfigured(Exception):
    pass


def estimate_cost(model, prompt_tokens, completion_tokens):
    price = PRICES.get(model)
    if not price:
        return None
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000


def extract_semantics(signal, timeout=None):
    """Call the LLM for one signal.

    Returns (fields_dict, model, prompt_tokens, completion_tokens, raw_text).
    fields_dict values are plain strings ("" when not found).
    Raises LLMNotConfigured when no API key; requests exceptions on HTTP errors.
    """
    if not API_KEY:
        raise LLMNotConfigured("OPENAI_API_KEY not set")

    timeout = timeout or int(os.environ.get("OPENAI_TIMEOUT", "180"))
    description = (signal.description or "")[:4000]
    # kimi-k2.x models reject temperature != 1; allow env override otherwise
    temperature = float(os.environ.get(
        "OPENAI_TEMPERATURE",
        "1" if MODEL.startswith("kimi") else "0",
    ))
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(
                signal_type=signal.signal_type,
                title=(signal.title or "")[:500],
                description=description or "(无正文)",
            )},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    resp = None
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json=payload,
                timeout=timeout,
            )
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == 2:
                raise
            log.warning("LLM attempt %d failed, retrying", attempt + 1)
    if resp.status_code >= 400:
        log.warning("LLM HTTP %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    body = resp.json()
    usage = body.get("usage") or {}
    raw = body["choices"][0]["message"]["content"]
    parsed = json.loads(raw)

    fields, evidence = {}, {}
    for key in ("pain_point", "audience", "scenario", "urgency",
                "current_solution", "alternatives", "supply_gap"):
        node = parsed.get(key) or {}
        value = (node.get("value") or "").strip()
        ev = (node.get("evidence") or "").strip()
        # enforce the evidence contract: no quote -> no value
        if value and not ev:
            value = ""
        fields[key] = value
        evidence[key] = ev

    return (fields, evidence, MODEL,
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), raw)
