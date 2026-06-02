"""
config.py — 全局配置中心

所有 API Key、模型参数、监控参数都集中在这里管理。
敏感信息（API Key）通过 .env 文件注入，绝不硬编码进源码，
这样代码可以安全地提交到 Git，而密钥留在本地。

本项目使用 DeepSeek API（OpenAI 兼容接口），用 openai SDK 调用。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（本文件所在目录），其他模块可复用该路径定位资源
BASE_DIR = Path(__file__).resolve().parent

# 加载同目录下的 .env 文件，把里面的键值对写入环境变量。
# override=True：让本项目 .env 的值优先于已存在的系统环境变量。
# 这很关键 —— 系统里可能残留一个全局 ANTHROPIC_BASE_URL（指向其他中转站），
# 不 override 的话它会劫持本项目配置，导致请求打到错误地址。
load_dotenv(BASE_DIR / ".env", override=True)


# ===================== DeepSeek API 配置 =====================

# 从环境变量读取 API Key；读不到时为 None，由调用方决定如何提示
API_KEY = os.getenv("API_KEY")

# 使用的模型。deepseek-chat 为通用对话模型，性价比高；
# 如需更强推理可改成 deepseek-reasoner
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "deepseek-chat")

# API 接入地址。DeepSeek 为 OpenAI 兼容接口，用 OpenAI SDK 调用，
# SDK 会自动拼接 '/chat/completions'，因此 base_url 以 '/v1' 结尾
# （最终请求 https://api.deepseek.com/v1/chat/completions）。
# DeepSeek 同时支持带/不带 /v1（此处的 /v1 与模型版本无关，仅为 OpenAI 兼容），
# 下面的归一化统一补成 /v1 结尾，避免 base_url 来源不同导致路径错误。
def _normalize_base_url(url: str) -> str:
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url


ANTHROPIC_BASE_URL = _normalize_base_url(
    os.getenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com")
)

# 单次分析允许的最大输出 token，控制成本与响应长度
MAX_TOKENS = 1024


# ===================== 数据抓取配置 =====================

# 涨速榜取前 N 名
TOP_N = 10

# 每只股票最多取多少条新闻 / 公告喂给模型，太多会增加成本且稀释重点
NEWS_LIMIT = 5
ANNOUNCEMENT_LIMIT = 5

# akshare 接口偶发超时，统一的重试次数与重试间隔（秒）
REQUEST_RETRIES = 3
RETRY_DELAY = 2


# ===================== 定时任务配置 =====================

# 涨速榜自动刷新间隔（秒）。盘中建议 30~60 秒，过于频繁可能被数据源限流
REFRESH_INTERVAL_SECONDS = 60


def validate_config() -> tuple[bool, str]:
    """启动前做一次配置自检，返回 (是否通过, 提示信息)。

    把校验逻辑收敛到一处，app.py / scheduler.py 都能复用，
    避免在缺少 Key 时才在调用 API 时抛出难以理解的异常。
    """
    if not API_KEY:
        return False, (
            "未检测到 API_KEY。请复制 .env.example 为 .env "
            "并填入你的 DeepSeek API Key。"
        )
    return True, "配置校验通过"
