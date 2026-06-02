"""
app.py — Streamlit 前端界面

运行：streamlit run app.py

界面布局：
  - 左侧/上方：实时涨速榜前 10 名表格（可手动刷新）。
  - 选中某只股票后：抓取其新闻+公告，调用 Claude 分析，展示上涨原因与利好程度。

性能/成本考量：
  - 涨速榜抓取用 st.cache_data 做短时缓存，避免每次交互都打数据源。
  - AI 分析只在用户点击「分析」时触发，不自动对 10 只股票全量调用，控制 API 成本。
"""

import streamlit as st

import config
import data_fetcher
from analyzer import analyze_stock

# 利好程度 -> 展示用的颜色 emoji，让等级在界面上一眼可辨
LEVEL_BADGE = {"高": "🔴 高", "中": "🟠 中", "低": "🟢 低", "未知": "⚪ 未知"}

st.set_page_config(page_title="A股涨速榜监控", page_icon="📈", layout="wide")


@st.cache_data(ttl=config.REFRESH_INTERVAL_SECONDS, show_spinner=False)
def load_top_movers(use_mock: bool):
    """带缓存地获取涨速榜，ttl 内重复调用直接返回缓存，减轻数据源压力。

    use_mock 作为缓存 key 的一部分：实时/模拟两种模式各自独立缓存，
    切换开关时不会串用对方的结果。返回 (movers, is_mock)。
    """
    return data_fetcher.fetch_top_movers(use_mock=use_mock)


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
    st.title("📈 A股实时涨速榜 + AI 分析")

    # 配置未就绪时给出明确提示，但仍允许查看榜单（榜单不需要 API Key）
    ok, msg = config.validate_config()
    if not ok:
        st.warning(msg)

    # 顶部工具栏：数据源开关 + 手动刷新
    col_mode, col_refresh, _ = st.columns([2, 1, 4])
    with col_mode:
        # 切换「模拟数据 / 实时数据」。开启后所有抓取都走 mock，方便无网络/演示。
        use_mock = st.toggle(
            "模拟数据模式",
            value=False,
            help="开启后使用内置模拟数据演示，不访问网络；关闭则抓取实时行情。",
        )
    with col_refresh:
        if st.button("🔄 刷新榜单"):
            load_top_movers.clear()  # 清掉缓存强制重新抓取

    movers, is_mock = load_top_movers(use_mock)
    if not movers:
        st.error("暂时无法获取涨速榜数据，请稍后点击『刷新榜单』重试，或开启模拟数据模式。")
        return

    # 明确告知当前数据来源：手动开了模拟，或实时接口失败被自动回退到模拟
    if is_mock and use_mock:
        st.info("当前为**模拟数据**模式（演示用，非实时行情）。")
    elif is_mock and not use_mock:
        st.warning("实时接口暂时不可用，已自动回退到**模拟数据**。可稍后刷新重试。")
    else:
        st.success("当前为**实时数据**。")

    left, right = st.columns([1, 1])

    with left:
        st.subheader(f"涨速榜 TOP {len(movers)}")
        # 用表格展示，并把 dict 列表整理成更友好的字段名
        table = [
            {
                "代码": m["code"],
                "名称": m["name"],
                "最新价": m["price"],
                "涨跌幅%": m["change_pct"],
                "涨速%": m["speed"],
            }
            for m in movers
        ]
        st.dataframe(table, use_container_width=True, hide_index=True)

        # 用下拉框选择要分析的股票（比给每行加按钮更简洁稳定）
        options = {f"{m['name']} ({m['code']})": m for m in movers}
        selected_label = st.selectbox("选择要分析的股票", list(options.keys()))
        do_analyze = st.button("🤖 AI 分析该股票", type="primary")

    with right:
        st.subheader("AI 分析结果")
        if do_analyze:
            target = options[selected_label]
            # 榜单是模拟数据时，新闻/公告也用模拟，保证演示流程一致
            render_analysis(target["code"], target["name"], use_mock=is_mock)
        else:
            st.info("在左侧选择一只股票后，点击『AI 分析该股票』查看结果。")


if __name__ == "__main__":
    main()
