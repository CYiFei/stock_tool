# stock_tool —— A股股息率追踪

一个轻量的个人 A 股股息率追踪网页应用：Flask + akshare + SQLite + Chart.js。用于记录自选股、按统一规则计算股息率，并叠加全市场行业观察与高股息筛选。

## 功能特性

- **自选股管理**：按 6 位代码添加股票，添加时可选「持有」或「感兴趣」两类；侧边栏快速索引，点击滚动定位。
- **股息率卡片**：每只股票展示最新股息率、近一年股息率走势折线图（纵轴 0.5% 刻度）、收盘价走势图。
- **手动记录**：支持手动录入/修正某日股息率（备注可选），自动记录与手动记录并存。
- **历年分红**：每只股票按年度聚合的每股分红柱状图 + 每年明细（按除权除息日）。
- **行业涨跌模块**：同花顺细分行业（90 个板块）实时涨跌榜，可切换涨幅/跌幅排序；点击某行业行展开该板块指数近 10 年走势。
- **高股息筛选模块**：全市场「股息率 > 3%」股票按行业分组展示，行业组可折叠，含现价与每股分红。

## 股息率计算规则

```
股息率 = 当年所有已实施分红（每股合计） ÷ 最新收盘价 × 100%
```

- 分红按**除权除息日所属年度**归集，仅统计进度为「实施」的现金分红。
- **当年（当前自然年）若无分红，则回退使用上一年度所有分红**。
- 每股派息 = 每 10 股派息额 ÷ 10。
- 该规则在个股卡片与全市场高股息筛选模块中保持一致。

## 技术栈与数据来源

| 用途 | 来源 |
| --- | --- |
| 股票代码 → 名称 | akshare `stock_info_a_code_name` |
| 个股分红历史 | akshare `stock_history_dividend_detail` |
| 个股日线收盘价 | akshare `stock_value_em` |
| 行业实时涨跌（90 细分行业） | akshare `stock_board_industry_summary_ths`（同花顺） |
| 板块指数近 10 年走势 | akshare `stock_board_industry_index_ths`（同花顺） |
| 板块名称 ↔ 代码映射 | akshare `stock_board_industry_name_ths` |
| 板块成分股 + 现价（高股息筛选用） | 同花顺 `q.10jqka.com.cn/thshy/detail/` 分页 |
| 全市场分红（高股息筛选用） | 东财数据中心 `RPT_SHAREBONUS_DET` |

> 注：本环境（2026 年）东财 push2 行情接口（`push2.eastmoney.com`）不可达，因此行业与成分股数据走同花顺、全市场分红走东财数据中心（`datacenter-web.eastmoney.com`）。两处均实测可用。

## 快速开始

```bash
pip install -r requirements.txt
bash run.sh        # 启动服务并自动打开浏览器
```

服务默认运行在 <http://127.0.0.1:5000>。

`run.sh` 等价于 `python3 app.py`（首次启动自动初始化 SQLite 数据库 `data.db`）。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 页面 |
| GET | `/api/stocks` | 自选股列表（含最新股息率、记录数） |
| POST | `/api/stocks` | 添加自选股，body `{code, interested}`，自动回填近一年历史 |
| DELETE | `/api/stocks/<code>` | 删除自选股及记录 |
| POST | `/api/stocks/<code>/interested` | 切换持有/感兴趣，body `{interested}` |
| GET | `/api/records/<code>` | 某只股票的股息率记录 |
| POST | `/api/records/<code>` | 手动录入/修正，body `{date, yield, note}` |
| DELETE | `/api/records/<code>?date=` | 删除某日记录 |
| POST | `/api/fetch` | 抓取最新交易日股息率（body 可选 `{code}` 指定单只，否则全部） |
| GET | `/api/dividends/<code>` | 历年分红（按年聚合） |
| GET | `/api/industries` | 同花顺细分行业实时涨跌（缓存 60s） |
| GET | `/api/industry/<name>` | 板块指数近 10 年走势（缓存 5min） |
| GET | `/api/dividend-screener` | 全市场股息率 > 3% 按行业分组（缓存 30min，`?refresh=1` 强制刷新） |

## 目录结构

```
stock_tool/
├── app.py              # Flask 后端：数据库、数据抓取、路由
├── templates/
│   └── index.html      # 单页前端（Chart.js 4.4.1 CDN）
├── data.db             # SQLite 数据库（自动生成）
├── requirements.txt
├── run.sh              # 启动脚本（打开浏览器）
└── README.md
```

## 常见问题

- **端口被占用**：`Address already in use` 说明有残留的 Flask 进程。找到并结束它：
  ```bash
  kill $(ss -tlnp | grep :5000 | grep -oP 'pid=\K[0-9]+' | head -1)
  ```
- **高股息筛选首次加载慢**：首次请求需扫描全部 90 个板块成分（约 20~30 秒），之后 30 分钟内命中缓存即时返回，也可点「刷新」强制更新。
- **数据源可用性**：A 股行情接口偶发波动，个股添加时的历史回填若失败会提示但股票仍会保留；「抓取今日全部」可重试。
