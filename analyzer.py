"""
analyzer.py — AI 分析层

把「股票名称 + 新闻 + 公告」喂给大模型（DeepSeek），让它判断：
  1. 这只股票可能的上涨原因；
  2. 利好程度（高 / 中 / 低）。

设计要点：
  - 用「系统提示词 + 结构化输出约定」让模型稳定返回可解析的结果。
  - 这里要求模型先输出一行 `利好程度: 高/中/低`，再输出分析正文，
    便于界面层用正则快速提取等级做高亮，无需依赖工具调用。
  - 任何 API 异常都被捕获，返回带 error 字段的结果，避免界面崩溃。
"""

import logging
import re

from openai import OpenAI

import config

logger = logging.getLogger(__name__)


# 系统提示词：界定角色、输出格式与边界（不构成投资建议）
SYSTEM_PROMPT = """你是一名严谨的A股市场分析助手。用户会给你某只股票的名称、近期新闻和公告。
请基于这些信息分析该股票短期上涨的可能原因，并判断消息面的利好程度。

严格按以下格式输出（第一行必须是利好程度，便于程序解析）：
利好程度: 高 或 中 或 低
上涨原因:
1. ……（结合具体新闻/公告，给出有依据的判断）
2. ……
风险提示: ……（如果信息不足以支撑上涨，要明确指出）

要求：
- 只依据给定材料推断，材料不足时如实说明，不要编造利好。
- 判断「高/中/低」的标准：高=有明确重大实质利好（如重组、超预期业绩、政策直接受益）；
  中=有一般性正面消息或行业景气；低=无明显利好或仅情绪/题材炒作。
- 这是信息分析，不是投资建议，不预测具体涨跌幅。"""


def _build_user_message(name: str, news: list[dict], announcements: list[dict]) -> str:
    """把抓取到的新闻、公告拼成结构清晰的文本喂给模型。

    用编号列表 + 明确小标题，让模型容易区分新闻与公告，提升分析质量。
    """
    parts = [f"股票名称：{name}", ""]

    parts.append("【近期新闻】")
    if news:
        for i, n in enumerate(news, 1):
            line = f"{i}. [{n.get('time', '')}] {n.get('title', '')}"
            content = (n.get("content") or "").strip()
            if content:
                # 控制单条长度，避免 token 浪费
                parts.append(line + f"\n   摘要：{content[:200]}")
            else:
                parts.append(line)
    else:
        parts.append("（暂无新闻）")

    parts.append("")
    parts.append("【近期公告】")
    if announcements:
        for i, a in enumerate(announcements, 1):
            parts.append(f"{i}. [{a.get('time', '')}] {a.get('title', '')}")
    else:
        parts.append("（暂无公告）")

    return "\n".join(parts)


def _extract_level(text: str) -> str:
    """从模型输出里提取利好程度（高/中/低），提取不到则返回『未知』。"""
    match = re.search(r"利好程度[:：]\s*([高中低])", text)
    return match.group(1) if match else "未知"


def analyze_stock(
    name: str, news: list[dict], announcements: list[dict]
) -> dict:
    """调用 DeepSeek 分析单只股票。

    返回 dict：
      {
        "level": "高/中/低/未知",
        "analysis": "模型输出的完整分析文本",
        "error": None 或 错误信息字符串,
      }
    """
    print(f"DEBUG - API_KEY: {config.API_KEY[:10]}... BASE_URL: {config.ANTHROPIC_BASE_URL} MODEL: {config.ANTHROPIC_MODEL}")
    # 先做配置自检，缺 Key 时直接返回友好错误，不发起请求
    ok, msg = config.validate_config()
    if not ok:
        return {"level": "未知", "analysis": "", "error": msg}

    # 新闻和公告都为空时，没有分析素材，避免浪费一次 API 调用
    if not news and not announcements:
        return {
            "level": "未知",
            "analysis": "暂无可用的新闻或公告，无法进行分析。",
            "error": None,
        }

    # DeepSeek 是 OpenAI 兼容接口，用 OpenAI SDK 调用。
    # 注意：OpenAI SDK 会把 '/chat/completions' 直接拼在 base_url 后面，
    # 所以 config.ANTHROPIC_BASE_URL 需以 '/v1' 结尾（最终 .../v1/chat/completions）。
    client = OpenAI(
        api_key=config.API_KEY,
        base_url=config.ANTHROPIC_BASE_URL,
    )
    user_message = _build_user_message(name, news, announcements)

    try:
        # OpenAI 格式没有独立的 system 参数，系统提示以 system 角色消息传入
        resp = client.chat.completions.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=config.MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )
    except Exception as err:  # noqa: BLE001 —— 网络/鉴权/限流等都在此兜底
        logger.error("调用 DeepSeek 失败：%s", err)
        return {"level": "未知", "analysis": "", "error": f"AI 分析调用失败：{err}"}

    # OpenAI 格式：内容在 choices[0].message.content；做个兜底防止为空
    text = (resp.choices[0].message.content or "").strip()

    return {"level": _extract_level(text), "analysis": text, "error": None}


if __name__ == "__main__":
    # 手动自测：python analyzer.py（需已配置 .env 中的 API Key）
    logging.basicConfig(level=logging.INFO)
    demo_news = [
        {"time": "2026-06-01", "title": "某公司中标10亿元大单", "content": "签订重大合同……"},
    ]
    result = analyze_stock("示例科技", demo_news, [])
    print("利好程度:", result["level"])
    print("错误:", result["error"])
    print(result["analysis"])
