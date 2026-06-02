# A股股票信息自动化监控系统

实时抓取东方财富涨速榜，结合个股新闻与公告，用 Claude 分析上涨原因与利好程度。

## 功能

- 📈 实时涨速榜 TOP 10（数据源：东方财富）
- 📰 个股最新新闻 + 公告抓取
- 🤖 Claude AI 分析上涨原因，判断利好程度（高 / 中 / 低）
- ⏱️ APScheduler 后台定时监控（可选）

## 项目结构

```
my-project/
├── config.py           # 配置中心（API Key、模型、监控参数）
├── data_fetcher.py     # akshare 数据抓取（涨速榜 / 新闻 / 公告）
├── analyzer.py         # 调用 Claude 做上涨原因与利好分析
├── app.py              # Streamlit 前端界面
├── scheduler.py        # APScheduler 后台定时监控（可选）
├── requirements.txt    # 依赖清单
├── .env.example        # 环境变量样例（复制为 .env 填入真实 Key）
└── .gitignore
```

## 快速开始

```bash
# 1. 安装依赖（Windows 用 py，其他系统用 python）
py -m pip install -r requirements.txt

# 2. 配置 API Key
copy .env.example .env        # macOS/Linux: cp .env.example .env
# 然后编辑 .env，填入 API_KEY（DeepSeek API Key）

# 3. 启动界面
streamlit run app.py

# 4.（可选）启动后台定时监控
py scheduler.py
```

## 模块自测

```bash
py data_fetcher.py   # 测试数据抓取（无需 API Key）
py analyzer.py       # 测试 AI 分析（需 API Key）
```

## 说明

- 本系统仅做信息聚合与分析，**不构成投资建议**。
- akshare 依赖实时网页接口，偶发超时已内置重试；若某接口持续失败，多为数据源
  接口变动，可升级 akshare：`py -m pip install -U akshare`。
