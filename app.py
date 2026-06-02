"""
app.py — Streamlit 前端界面

运行：streamlit run app.py

界面布局：
  - 顶部：异动提醒区（涨速突然飙升时弹红色警告，历史保留）。
  - 左侧：实时涨速榜前 10 名表格；自动分析模式下每行带利好程度标签。
  - 右侧：选中某只股票后展示完整 AI 分析。

性能/成本考量：
  - 涨速榜抓取用 st.cache_data 做短时缓存，避免每次交互都打数据源。
  - 自动分析对 TOP10 全量调用 AI，成本较高，因此用「快照签名」做闸门：
    同一批榜单只分析一次，避免每次界面交互（rerun）都重复烧钱。
  - 关闭自动分析时，恢复原来的「手动点击单只分析」模式。
"""

import time

import pandas as pd
import streamlit as st

import config
import data_fetcher
from analyzer import analyze_stock

# 利好程度 -> 展示用的颜色 emoji，让等级在界面上一眼可辨
LEVEL_BADGE = {"高": "🔴 高", "中": "🟠 中", "低": "🟢 低", "未知": "⚪ 未知"}

# 利好程度 -> 表格单元格背景色（用于自动分析模式下给每行打标签）
LEVEL_COLORS = {"高": "#ff4b4b", "中": "#ffa500", "低": "#21c354"}

# 涨速「异动」阈值：相比上次快照提升超过该百分点即触发提醒
SPEED_ALERT_THRESHOLD = 0.5

st.set_page_config(page_title="A股涨速榜监控", page_icon="📈", layout="wide")


def _init_session_state():
    """初始化所有跨 rerun 需要保留的状态，避免首次访问时 KeyError。"""
    ss = st.session_state
    ss.setdefault("analysis_results", {})   # code -> {"level","analysis"}，自动分析结果
    ss.setdefault("analyzed_sig", None)      # 上述结果对应的榜单签名（代码集合）
    ss.setdefault("prev_speeds", {})         # code -> 上一次快照的涨速，用于异动对比
    ss.setdefault("alert_sig", None)         # 上一次做过异动对比的快照签名（代码+涨速）
    ss.setdefault("alert_history", [])       # 异动提醒历史（最新在前），不被覆盖


def _to_float(v):
    """把涨速/价格等可能是字符串或 None 的值安全转成 float，失败返回 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _codes_sig(movers: list[dict]) -> tuple:
    """榜单「代码集合」签名：只要上榜股票没变就视为同一批，用于给自动分析做闸门。"""
    return tuple(m["code"] for m in movers)


def _full_sig(movers: list[dict]) -> tuple:
    """榜单「代码+涨速」签名：涨速一变就算新快照，用于触发异动对比。"""
    return tuple((m["code"], round(_to_float(m["speed"]) or 0.0, 3)) for m in movers)


@st.cache_data(ttl=config.REFRESH_INTERVAL_SECONDS, show_spinner=False)
def load_top_movers(use_mock: bool):
    """带缓存地获取涨速榜，ttl 内重复调用直接返回缓存，减轻数据源压力。

    use_mock 作为缓存 key 的一部分：实时/模拟两种模式各自独立缓存，
    切换开关时不会串用对方的结果。返回 (movers, is_mock)。
    """
    return data_fetcher.fetch_top_movers(use_mock=use_mock)


# ===================== 功能2：异动提醒 =====================

def detect_speed_alerts(movers: list[dict]):
    """对比上一次快照，检测涨速突然飙升的股票并追加到提醒历史。

    逻辑：
      1. 用「代码+涨速」签名做闸门 —— 同一批数据只比对一次，避免每次 rerun
         （比如切个开关）都重复触发同样的提醒。
      2. 对每只股票，用本次涨速减去上次记录的涨速，差值 > 阈值即为异动。
      3. 命中的提醒以「最新在前」追加进 alert_history，历史保留不覆盖。
      4. 最后把本次涨速存为 prev_speeds，作为下次对比的基准。
    """
    ss = st.session_state
    sig = _full_sig(movers)
    # 签名没变说明还是同一批数据，不重复对比（但不清空已有历史）
    if sig == ss.alert_sig:
        return
    prev = ss.prev_speeds

    for m in movers:
        cur = _to_float(m["speed"])
        old = prev.get(m["code"])
        if cur is None or old is None:
            continue  # 首次出现的股票没有基准，跳过本轮
        delta = cur - old
        if delta > SPEED_ALERT_THRESHOLD:
            ss.alert_history.insert(0, {
                "time": time.strftime("%H:%M:%S"),
                "name": m["name"],
                "code": m["code"],
                "delta": delta,
                "speed": cur,
            })

    # 更新基准与签名，供下次刷新对比
    ss.prev_speeds = {m["code"]: _to_float(m["speed"]) for m in movers}
    ss.alert_sig = sig


def render_alerts():
    """在页面顶部渲染异动提醒历史（红色警告框，最新在前，不覆盖旧的）。"""
    history = st.session_state.alert_history
    if not history:
        return
    st.markdown("#### 🚨 异动提醒")
    for a in history:
        # 格式：⚠️ 异动提醒：XX股票涨速突然飙升 +X.X%，请关注
        st.error(
            f"⚠️ 异动提醒：{a['name']} 涨速突然飙升 +{a['delta']:.1f}%"
            f"（当前 {a['speed']:.2f}%，{a['time']}），请关注"
        )


# ===================== 功能1：自动分析全部榜单 =====================

def run_auto_analysis(movers: list[dict], use_mock: bool):
    """对 TOP N 每只股票依次抓取新闻/公告并调用 AI 分析，带进度条。

    成本控制：用「代码集合」签名做闸门 —— 只要上榜股票不变，就复用上次的
    分析结果，不重复调用 API。只有刷新出新榜单（成分变化）时才重新分析。
    结果存进 session_state.analysis_results（code -> {level, analysis}）。
    """
    ss = st.session_state
    sig = _codes_sig(movers)
    if sig == ss.analyzed_sig and ss.analysis_results:
        return  # 同一批榜单已分析过，直接用缓存结果

    results = {}
    progress = st.progress(0.0, text="开始自动分析 TOP 榜单……")
    total = len(movers)
    for i, m in enumerate(movers, 1):
        progress.progress(
            (i - 1) / total,
            text=f"正在分析 {m['name']}（{i}/{total}）……",
        )
        news = data_fetcher.fetch_stock_news(m["code"], use_mock=use_mock)
        ann = data_fetcher.fetch_stock_announcements(m["code"], use_mock=use_mock)
        result = analyze_stock(m["name"], news, ann)
        results[m["code"]] = {
            "level": result["level"],
            "analysis": result["analysis"],
            "error": result["error"],
        }
    progress.progress(1.0, text="分析完成 ✅")
    progress.empty()  # 完成后移除进度条

    ss.analysis_results = results
    ss.analyzed_sig = sig


def _style_level_column(df: pd.DataFrame):
    """给表格的「利好程度」列按等级上色，返回 Styler 供 st.dataframe 渲染。"""
    def color(val):
        bg = LEVEL_COLORS.get(str(val).strip())
        return f"background-color:{bg};color:white;font-weight:bold" if bg else ""

    return df.style.map(color, subset=["利好程度"])


def render_analysis(code: str, name: str, use_mock: bool):
    """抓取指定股票的新闻+公告并调用 AI 分析，把结果渲染到界面。

    use_mock=True 时新闻/公告也使用模拟数据，保证演示流程可完整跑通。
    """
    with st.spinner(f"正在抓取 {name} 的新闻与公告……"):
        news = data_fetcher.fetch_stock_news(code, use_mock=use_mock)
        announcements = data_fetcher.fetch_stock_announcements(code, use_mock=use_mock)

    st.write(f"抓取到 **{len(news)}** 条新闻、**{len(announcements)}** 条公告。")

    with st.spinner("AI 正在分析上涨原因……"):
        result = analyze_stock(name, news, announcements)

    if result["error"]:
        st.error(result["error"])
        return

    st.subheader(f"利好程度：{LEVEL_BADGE.get(result['level'], result['level'])}")
    st.markdown(result["analysis"])

    # 把原始素材折叠展示，方便用户核对 AI 的判断依据
    with st.expander("查看原始新闻 / 公告"):
        st.markdown("**新闻**")
        for n in news:
            st.markdown(f"- [{n['time']}] {n['title']}")
        st.markdown("**公告**")
        for a in announcements:
            title = f"[{a['time']}] {a['title']}"
            st.markdown(f"- [{title}]({a['url']})" if a.get("url") else f"- {title}")


def main():
    _init_session_state()
    st.title("📈 A股实时涨速榜 + AI 分析")

    # 配置未就绪时给出明确提示，但仍允许查看榜单（榜单不需要 API Key）
    ok, msg = config.validate_config()
    if not ok:
        st.warning(msg)

    # 顶部工具栏：数据源开关 + 自动分析开关 + 手动刷新
    col_mode, col_auto, col_refresh, _ = st.columns([2, 2, 1, 3])
    with col_mode:
        # 切换「模拟数据 / 实时数据」。开启后所有抓取都走 mock，方便无网络/演示。
        use_mock = st.toggle(
            "模拟数据模式",
            value=False,
            help="开启后使用内置模拟数据演示，不访问网络；关闭则抓取实时行情。",
        )
    with col_auto:
        # 自动分析模式：刷新后自动对 TOP10 全量 AI 分析；关闭则恢复手动单只分析。
        auto_mode = st.toggle(
            "自动分析模式",
            value=False,
            help="开启后刷新榜单会自动对 TOP10 逐只调用 AI 分析（成本较高）。",
        )
    with col_refresh:
        if st.button("🔄 刷新榜单"):
            load_top_movers.clear()  # 清掉缓存强制重新抓取

    movers, is_mock = load_top_movers(use_mock)
    if not movers:
        st.error("暂时无法获取涨速榜数据，请稍后点击『刷新榜单』重试，或开启模拟数据模式。")
        return

    # 功能2：每次拿到榜单先做异动对比（内部用签名闸门，同批数据只比一次）
    detect_speed_alerts(movers)
    # 异动提醒渲染在页面顶部，历史保留
    render_alerts()

    # 明确告知当前数据来源：手动开了模拟，或实时接口失败被自动回退到模拟
    if is_mock and use_mock:
        st.info("当前为**模拟数据**模式（演示用，非实时行情）。")
    elif is_mock and not use_mock:
        st.warning("实时接口暂时不可用，已自动回退到**模拟数据**。可稍后刷新重试。")
    else:
        st.success("当前为**实时数据**。")

    # 功能1：自动分析模式下，对整个榜单跑一遍 AI 分析（带进度条，签名闸门防重复）
    if auto_mode:
        run_auto_analysis(movers, use_mock=is_mock)

    left, right = st.columns([1, 1])

    with left:
        st.subheader(f"涨速榜 TOP {len(movers)}")
        # 把 dict 列表整理成更友好的字段名
        rows = [
            {
                "代码": m["code"],
                "名称": m["name"],
                "最新价": m["price"],
                "涨跌幅%": m["change_pct"],
                "涨速%": m["speed"],
            }
            for m in movers
        ]

        if auto_mode:
            # 自动分析模式：每行右边追加「利好程度」标签列并按等级上色
            results = st.session_state.analysis_results
            for row, m in zip(rows, movers):
                row["利好程度"] = results.get(m["code"], {}).get("level", "未知")
            df = pd.DataFrame(rows)
            st.dataframe(
                _style_level_column(df),
                use_container_width=True,
                hide_index=True,
            )
        else:
            # 手动模式：普通表格
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # 选择要查看详情的股票
        options = {f"{m['name']} ({m['code']})": m for m in movers}
        selected_label = st.selectbox("选择要查看的股票", list(options.keys()))
        # 手动模式才需要这个分析按钮；自动模式下点了也能看完整分析
        do_analyze = st.button("🤖 AI 分析该股票", type="primary")

    with right:
        st.subheader("AI 分析结果")
        target = options[selected_label]
        cached = st.session_state.analysis_results.get(target["code"])

        if auto_mode and cached:
            # 自动模式：直接展示该股票已缓存的完整分析，无需再次调用
            if cached["error"]:
                st.error(cached["error"])
            else:
                st.subheader(
                    f"利好程度：{LEVEL_BADGE.get(cached['level'], cached['level'])}"
                )
                st.markdown(cached["analysis"])
        elif do_analyze:
            # 手动模式（或自动结果缺失）：现场抓取+分析单只股票
            render_analysis(target["code"], target["name"], use_mock=is_mock)
        else:
            st.info("在左侧选择一只股票后，点击『AI 分析该股票』查看结果。")


if __name__ == "__main__":
    main()
