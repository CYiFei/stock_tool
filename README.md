# stock_tool —— A股股息率追踪

一个轻量的个人 A 股股息率追踪网页应用：Flask + akshare + SQLite + Chart.js。用于记录自选股、按统一规则计算股息率，并叠加全市场行业观察与高股息筛选。

## 功能特性

- **自选股管理**：按 6 位代码添加股票，添加时可选「持有」或「感兴趣」两类；侧边栏快速索引，点击滚动定位。
- **股息率卡片**：每只股票展示最新股息率、近一年股息率走势折线图（纵轴 0.5% 刻度）、收盘价走势图。
- **手动记录**：支持手动录入/修正某日股息率（备注可选），自动记录与手动记录并存。
- **历年分红**：每只股票按年度聚合的每股分红柱状图 + 每年明细（按除权除息日）。
- **行业涨跌模块**：同花顺细分行业（90 个板块）实时涨跌榜，可切换涨幅/跌幅排序；点击某行业行展开该板块指数近 10 年走势。
- **高股息筛选模块**：全市场「股息率 > 3%」股票按行业分组展示，行业组可折叠，含现价与每股分红。
- **三级行业下跌比例**：独立页面 `/industry-decline`，统计同花顺 90 个三级行业内「近 1~10 年」下跌股票占比热力表，可排序/筛选，点击行业行展开成分股各周期涨跌幅明细。

## 股息率计算规则

```
股息率 = 当年所有已实施分红（每股合计） ÷ 最新收盘价 × 100%
```

- 分红按**除权除息日所属年度**归集，仅统计进度为「实施」的现金分红。
- **当年（当前自然年）若无分红，则回退使用上一年度所有分红**。
- 每股派息 = 每 10 股派息额 ÷ 10。
- 该规则在个股卡片与全市场高股息筛选模块中保持一致。

## 三级行业下跌比例（/industry-decline）

统计同花顺 90 个三级行业（细分行业）中，收盘价低于 1~10 年前的股票占比：

- **涨跌幅口径**：腾讯月K收盘价，**后复权**（含历史分红送转，近似总回报）。
  不用前复权——腾讯前复权对高分红股会算出负的历史价，涨跌幅失真。
- **基准价** = 个股最近一根月K收盘（含当月最新价）；「近 N 年」= 基准日减 N 个自然年前最后一根月K收盘。
- **下跌比例** = 近 N 年涨跌幅为负的股票数 ÷ 该行业 N 年前已上市的股票数（上市不足 N 年不计入该列分母）。
- **局限**：成分股为当前名单，已退市个股不在统计内（幸存者偏差）；北交所（92 开头）个股因数据源无月K未计入。
- **构建**：首次/重建需逐股抓取约 5100 只个股月K，数据源限速约 10 条/秒，全量约 10 分钟；
  内置开局/滚动失败率熔断，数据源被限流时自动中止并提示，稍后重试即可。
  构建结果缓存在 `data.db`（表 `idc_members` / `idc_month_close` / `idc_meta`），重启后无需重抓。

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
| 三级行业成分（下跌比例用） | 同花顺 `q.10jqka.com.cn/thshy/`（直连） |
| 个股月K收盘价 + 现价（下跌比例用） | 腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get`（后复权月K，直连） |
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
| GET | `/industry-decline` | 三级行业下跌比例页面 |
| GET | `/api/industry-decline` | 三级行业下跌比例数据（未构建时自动触发后台构建，返回 202） |
| GET | `/api/industry-decline/status` | 构建进度 |
| POST | `/api/industry-decline/refresh` | 强制重建（后台，约 10 分钟） |

## 目录结构

```
stock_tool/
├── app.py              # Flask 后端：数据库、数据抓取、路由
├── industry_decline.py # 三级行业下跌比例：数据管道、缓存、蓝图路由
├── templates/
│   ├── index.html          # 单页前端（Chart.js 4.4.1 CDN）
│   └── industry_decline.html # 三级行业下跌比例页面
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
- **三级行业下跌比例首次构建慢**：需逐股抓取约 5100 只个股月K，源站限速约 10 条/秒，全量约 10 分钟；期间可离开页面，构建在后台完成。若数据源临时限流（腾讯 WAF），构建会自动熔断中止，等几分钟后点「重建数据」重试即可；旧数据在重建完成前仍可正常查看。
- **数据源可用性**：A 股行情接口偶发波动，个股添加时的历史回填若失败会提示但股票仍会保留；「抓取今日全部」可重试。
