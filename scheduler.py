"""
scheduler.py — 定时任务（APScheduler）

用途：在后台周期性抓取涨速榜，做「实时监控」。
可独立运行：python scheduler.py
也可被其他程序导入后调用 start_scheduler() 启动。

说明：
  - 这是一个可选的后台监控进程，与 Streamlit 界面相互独立。
    界面靠 st.cache_data 的 ttl 来「按需刷新」；本进程则是「主动轮询」，
    适合需要无人值守持续抓取（比如把异动写日志、后续接入告警）的场景。
  - 默认用阻塞式 BlockingScheduler，单独跑一个终端即可。
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

import config
import data_fetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("scheduler")


def monitor_job():
    """单次监控任务：抓取涨速榜并打印前几名。

    这里只做最小演示（打印），实际可在此扩展：
      - 涨速超过阈值时记录/告警；
      - 对榜首股票自动触发 analyzer.analyze_stock 做 AI 分析。
    """
    movers, is_mock = data_fetcher.fetch_top_movers()
    if not movers:
        logger.warning("本轮未获取到涨速榜数据")
        return
    logger.info("涨速榜 TOP%d%s：", len(movers), "（模拟数据）" if is_mock else "")
    for m in movers:
        logger.info("  %s(%s) 涨速 %s%% 涨幅 %s%%",
                    m["name"], m["code"], m["speed"], m["change_pct"])


def start_scheduler():
    """启动定时调度器，按 config.REFRESH_INTERVAL_SECONDS 间隔执行 monitor_job。"""
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        monitor_job,
        trigger="interval",
        seconds=config.REFRESH_INTERVAL_SECONDS,
        next_run_time=None,  # 不立即执行，等第一个间隔到点再跑；想立即跑可删此行
        id="monitor_top_movers",
    )
    logger.info("调度器启动，每 %d 秒抓取一次涨速榜。Ctrl+C 退出。",
                config.REFRESH_INTERVAL_SECONDS)
    monitor_job()  # 启动时先立即跑一次，避免等待第一个间隔
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止。")


if __name__ == "__main__":
    start_scheduler()
