"""
data_fetcher.py — 数据抓取层

封装所有对 akshare 的调用，向上层（analyzer / app）提供干净的 Python 数据结构。
设计原则：
  1. 任何一个接口失败都不应让整个程序崩溃 —— 失败时返回空数据并记录日志，
     让界面层可以优雅降级（比如新闻取不到，仍能用公告做分析）。
  2. akshare 底层是实时网页接口，偶发超时，因此统一加了重试。
  3. akshare 的列名偶尔会变，这里用「列名兜底」的方式读取，尽量不写死索引。

涉及的 akshare 接口：
  - ak.stock_zh_a_spot_em()                    沪深京A股实时行情（含「涨速」列）
  - ak.stock_news_em(symbol=代码)               东方财富-个股新闻
  - ak.stock_notice_report(symbol=类型, date=)  公告大全（按日期，全市场）
"""

import logging
import random
import time
from typing import Any

import akshare as ak
import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

# 部分网络环境下，东方财富会拒绝不带浏览器 UA 的请求（直接断开连接），
# 而 akshare 默认请求有时正好踩中这一点。直连兜底时统一带上浏览器 UA。
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def _retry(func, *args, **kwargs):
    """带重试的调用包装。

    akshare 接口偶发网络抖动，这里统一重试 config.REQUEST_RETRIES 次。
    全部失败后抛出最后一次的异常，由各 fetch_* 函数捕获并降级处理。
    """
    last_err: Exception | None = None
    for attempt in range(1, config.REQUEST_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as err:  # noqa: BLE001 —— 数据源异常类型多样，统一兜底
            last_err = err
            logger.warning(
                "调用 %s 第 %d/%d 次失败：%s",
                getattr(func, "__name__", str(func)),
                attempt,
                config.REQUEST_RETRIES,
                err,
            )
            if attempt < config.REQUEST_RETRIES:
                time.sleep(config.RETRY_DELAY)
    assert last_err is not None
    raise last_err


def _pick(row: pd.Series, *candidates: str, default: Any = "") -> Any:
    """从一行数据里按候选列名依次取值，取到第一个存在的就返回。

    用于兼容 akshare 不同版本的列名差异（例如「新闻标题」/「标题」）。
    """
    for name in candidates:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return default


# ===================== 模拟（演示）数据 =====================
# 用途：① 真实接口全部失败时自动兜底；② 用户在界面手动开启「模拟数据」做演示。
# 说明：股票名称均为真实存在的A股，价格/涨速/涨跌幅是合理的「演示数值」而非实时
#       行情，仅用于无网络或演示场景下跑通完整流程。列表已按「涨速」降序排列。
_MOCK_TOP_MOVERS: list[dict] = [
    {"code": "300750", "name": "宁德时代", "price": 245.80, "change_pct": 8.65, "speed": 2.35},
    {"code": "002594", "name": "比亚迪",   "price": 268.50, "change_pct": 6.20, "speed": 1.98},
    {"code": "300059", "name": "东方财富", "price": 18.42,  "change_pct": 9.80, "speed": 1.75},
    {"code": "601012", "name": "隆基绿能", "price": 22.15,  "change_pct": 5.40, "speed": 1.52},
    {"code": "002415", "name": "海康威视", "price": 31.88,  "change_pct": 4.75, "speed": 1.30},
    {"code": "600519", "name": "贵州茅台", "price": 1685.00, "change_pct": 3.20, "speed": 1.10},
    {"code": "000858", "name": "五粮液",   "price": 142.60, "change_pct": 3.85, "speed": 0.95},
    {"code": "000333", "name": "美的集团", "price": 72.30,  "change_pct": 2.90, "speed": 0.82},
    {"code": "600036", "name": "招商银行", "price": 38.75,  "change_pct": 2.15, "speed": 0.68},
    {"code": "601318", "name": "中国平安", "price": 51.20,  "change_pct": 1.95, "speed": 0.55},
]

# 代码 -> 名称 的反查表，供 mock 新闻/公告在只拿到 code 时补全名称
_MOCK_NAME_BY_CODE = {m["code"]: m["name"] for m in _MOCK_TOP_MOVERS}


def get_mock_top_movers(top_n: int = config.TOP_N) -> list[dict]:
    """返回模拟涨速榜（已按涨速降序），最多 top_n 条。

    每次调用都会给每只股票的涨速叠加一个 -0.3 ~ +0.8 的随机扰动，
    这样反复刷新时涨速会变化，便于演示「异动提醒」的触发。
    扰动偏向正向（上界 0.8 > 下界 0.3），更容易越过提醒阈值。
    扰动后重新按涨速降序排序，保证榜单仍是有序的涨速榜；涨速保留两位小数。

    返回深拷贝，避免上层无意修改污染常量。
    """
    import copy

    movers = copy.deepcopy(_MOCK_TOP_MOVERS[:top_n])
    for m in movers:
        jitter = random.uniform(-0.3, 0.8)
        # 叠加扰动并兜底不为负（涨速可正可负，这里演示用，限制最低 0）
        m["speed"] = round(max(0.0, m["speed"] + jitter), 2)
    # 扰动可能打乱原有顺序，重新降序排列，维持「涨速榜」语义
    movers.sort(key=lambda m: m["speed"], reverse=True)
    return movers


def get_mock_news(code: str, name: str | None = None,
                  limit: int = config.NEWS_LIMIT) -> list[dict]:
    """生成某只股票的模拟新闻（演示用，内容为通用正面模板）。"""
    name = name or _MOCK_NAME_BY_CODE.get(code, code)
    templates = [
        {"title": f"{name}发布最新经营数据，季度营收同比增长", "source": "演示来源",
         "content": f"{name}公布的最新经营情况显示主营业务保持增长，机构普遍给予正面评价。"},
        {"title": f"多家机构上调{name}评级至『买入』", "source": "演示来源",
         "content": f"近期有多家券商研报上调{name}目标价，看好其行业地位与盈利前景。"},
        {"title": f"{name}所在行业景气度回升，板块获资金关注", "source": "演示来源",
         "content": f"受行业需求改善推动，{name}所在板块整体走强，成交活跃。"},
        {"title": f"{name}获纳入重要指数样本股", "source": "演示来源",
         "content": f"{name}被纳入相关宽基指数样本，有望带来增量配置资金。"},
        {"title": f"{name}投资者关系活动记录披露，回应市场关切", "source": "演示来源",
         "content": f"{name}在最新交流中就产能、订单等热点问题作出回应，态度积极。"},
    ]
    # 给每条补一个递减的演示时间，越靠前越新
    for i, item in enumerate(templates):
        item["time"] = f"2026-06-0{max(1, 5 - i)} 09:3{i}:00"
    return templates[:limit]


def get_mock_announcements(code: str, name: str | None = None,
                           limit: int = config.ANNOUNCEMENT_LIMIT) -> list[dict]:
    """生成某只股票的模拟公告（演示用）。"""
    name = name or _MOCK_NAME_BY_CODE.get(code, code)
    templates = [
        {"title": f"{name}：关于2026年第一季度业绩预告的公告", "url": ""},
        {"title": f"{name}：关于回购公司股份进展的公告", "url": ""},
        {"title": f"{name}：关于签订重大合作框架协议的公告", "url": ""},
        {"title": f"{name}：关于召开2025年年度股东大会的通知", "url": ""},
        {"title": f"{name}：关于使用部分闲置自有资金进行现金管理的公告", "url": ""},
    ]
    for i, item in enumerate(templates):
        item["time"] = f"2026-06-0{max(1, 5 - i)}"
    return templates[:limit]


# ===================== 1. 实时涨速榜 =====================

def _fetch_top_movers_direct(top_n: int) -> list[dict]:
    """直连东方财富行情接口获取涨速榜（akshare 失败时的兜底方案）。

    东方财富字段约定：f14=名称 f12=代码 f2=最新价 f3=涨跌幅 f22=涨速。
    fid=f22 表示按涨速排序，po=1 表示降序，因此返回的就是涨速榜。
    带浏览器 UA 以规避「无 UA 被拒绝」的情况。
    """
    params = {
        "pn": 1, "pz": top_n, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f22",  # 按涨速排序
        # 沪深京A股板块组合（主板/创业板/科创板/北交所）
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "fields": "f12,f14,f2,f3,f22",
    }
    # 东方财富有多个行情域名做负载，逐个尝试提升成功率
    hosts = [
        "https://82.push2.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/api/qt/clist/get",
    ]
    last_err: Exception | None = None
    for host in hosts:
        try:
            resp = requests.get(
                host, params=params, headers=_BROWSER_HEADERS, timeout=10
            )
            resp.raise_for_status()
            diff = (resp.json().get("data") or {}).get("diff") or []
            movers = [
                {
                    "code": str(x.get("f12", "")),
                    "name": str(x.get("f14", "")),
                    "price": x.get("f2"),
                    "change_pct": x.get("f3"),
                    "speed": x.get("f22"),
                }
                for x in diff[:top_n]
            ]
            if movers:
                return movers
        except Exception as err:  # noqa: BLE001
            last_err = err
            logger.warning("直连 %s 获取涨速榜失败：%s", host, err)
    if last_err:
        logger.error("直连兜底也失败：%s", last_err)
    return []


def fetch_top_movers(
    top_n: int = config.TOP_N, use_mock: bool = False
) -> tuple[list[dict], bool]:
    """获取东方财富实时涨速榜前 N 名。

    参数：
      top_n：取前几名。
      use_mock：True 则直接返回模拟数据（演示模式），不访问网络。

    实现思路（use_mock=False 时）：
      首选 akshare 的 stock_zh_a_spot_em()（返回全市场快照，含「涨速」列），
      按「涨速」降序取前 N。若该接口在当前网络环境被拒绝/超时，则自动
      切换到 _fetch_top_movers_direct() 直连东方财富接口兜底；若仍失败，
      最终回退到模拟数据，保证界面始终有内容可展示。

    返回：(movers, is_mock)
      movers：每只股票一个 dict（代码/名称/最新价/涨跌幅/涨速）。
      is_mock：本次返回的是否为模拟数据，供界面提示用户。
    """
    if use_mock:
        return get_mock_top_movers(top_n), True

    try:
        df = _retry(ak.stock_zh_a_spot_em)
    except Exception as err:  # noqa: BLE001
        logger.warning("akshare 获取行情失败，改用直连兜底：%s", err)
        df = None

    if df is None or df.empty or "涨速" not in df.columns:
        if df is not None and not df.empty and "涨速" not in df.columns:
            logger.warning("akshare 行情缺少『涨速』列，改用直连兜底")
        movers = _fetch_top_movers_direct(top_n)
        if movers:
            return movers, False
        # akshare 与直连都失败，回退到模拟数据兜底
        logger.error("真实行情接口全部失败，回退到模拟数据")
        return get_mock_top_movers(top_n), True

    # 涨速可能含 NaN，先转数值再排序，避免排序异常
    df = df.copy()
    df["涨速"] = pd.to_numeric(df["涨速"], errors="coerce")
    df = df.dropna(subset=["涨速"]).sort_values("涨速", ascending=False).head(top_n)

    movers = []
    for _, row in df.iterrows():
        movers.append(
            {
                "code": str(_pick(row, "代码")),
                "name": str(_pick(row, "名称")),
                "price": _pick(row, "最新价", default=None),
                "change_pct": _pick(row, "涨跌幅", default=None),
                "speed": _pick(row, "涨速", default=None),
            }
        )
    return movers, False


# ===================== 2. 个股新闻 =====================

def fetch_stock_news(
    code: str, limit: int = config.NEWS_LIMIT, use_mock: bool = False
) -> list[dict]:
    """获取指定股票的最新新闻（东方财富个股新闻）。

    参数 code：6 位股票代码，如 '300059'（不带交易所前缀）。
    参数 use_mock：True 则返回模拟新闻（演示模式），不访问网络。
    返回：最多 limit 条新闻 dict（标题/内容/时间/来源）。失败返回空列表。
    """
    if use_mock:
        return get_mock_news(code, limit=limit)

    try:
        df = _retry(ak.stock_news_em, symbol=code)
    except Exception as err:  # noqa: BLE001
        logger.warning("获取 %s 新闻失败：%s", code, err)
        return []

    if df is None or df.empty:
        return []

    df = df.head(limit)
    news: list[dict] = []
    for _, row in df.iterrows():
        news.append(
            {
                "title": str(_pick(row, "新闻标题", "标题")),
                "content": str(_pick(row, "新闻内容", "内容")),
                "time": str(_pick(row, "发布时间", "时间")),
                "source": str(_pick(row, "文章来源", "来源")),
            }
        )
    return news


# ===================== 3. 个股公告 =====================

def fetch_stock_announcements(
    code: str, limit: int = config.ANNOUNCEMENT_LIMIT, use_mock: bool = False
) -> list[dict]:
    """获取指定股票的最新公告（巨潮资讯-个股信息披露）。

    使用 ak.stock_zh_a_disclosure_report_cninfo，按代码拉取信息披露报告。
    该接口需要时间区间，这里默认取最近一年；market 传 '沪深京' 覆盖主要市场。

    参数 use_mock：True 则返回模拟公告（演示模式），不访问网络。
    返回：最多 limit 条公告 dict（标题/时间/链接）。失败返回空列表。
    """
    if use_mock:
        return get_mock_announcements(code, limit=limit)

    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=365)

    try:
        df = _retry(
            ak.stock_zh_a_disclosure_report_cninfo,
            symbol=code,
            market="沪深京",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as err:  # noqa: BLE001
        logger.warning("获取 %s 公告失败：%s", code, err)
        return []

    if df is None or df.empty:
        return []

    df = df.head(limit)
    announcements: list[dict] = []
    for _, row in df.iterrows():
        announcements.append(
            {
                "title": str(_pick(row, "公告标题", "标题")),
                "time": str(_pick(row, "公告时间", "时间", "公告日期")),
                "url": str(_pick(row, "公告链接", "链接", "网址")),
            }
        )
    return announcements


if __name__ == "__main__":
    # 手动自测：python data_fetcher.py
    logging.basicConfig(level=logging.INFO)
    top, is_mock = fetch_top_movers()
    print(f"涨速榜前 {len(top)} 名（{'模拟数据' if is_mock else '实时数据'}）：")
    for item in top:
        print(f"  {item['name']}({item['code']})  涨速 {item['speed']}  "
              f"涨幅 {item['change_pct']}%")
    if top:
        first = top[0]
        print(f"\n抓取 {first['name']} 的新闻与公告（use_mock={is_mock}）……")
        print("新闻：", len(fetch_stock_news(first["code"], use_mock=is_mock)), "条")
        print("公告：", len(fetch_stock_announcements(first["code"], use_mock=is_mock)), "条")
