#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股每日股息率追踪工具 —— Flask 后端。

数据来源 akshare：
  - 股票名称：stock_info_a_code_name()  （代码->名称映射，缓存）
  - 派息历史：stock_history_dividend_detail(symbol, '分红')  （派息/10股、除权除息日、进度）
  - 日线收盘价：stock_value_em(symbol)  （数据日期、当日收盘价）

股息率 = 当前年度所有已实施分红（按除权除息日、进度='实施'）每股派息之和 ÷ 最新收盘价 × 100%
        若当前年度尚无分红，则使用上一年度所有分红
"""

import os
import time
import sqlite3
import requests
from datetime import datetime, timedelta
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup

# 先把 tqdm 静音，再导入 akshare，使 akshare 内部 `from tqdm import tqdm` 拿到静音版本，
# 避免 stock_info_a_code_name 分页进度条刷屏。
import tqdm as _tqdm
_orig_tqdm = _tqdm.tqdm
def _quiet_tqdm(*a, **k):
    return _orig_tqdm(*a, **{**k, "disable": True})
_tqdm.tqdm = _quiet_tqdm

import akshare as ak
import pandas as pd
from flask import Flask, request, jsonify, render_template

from industry_decline import bp as industry_decline_bp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")

app = Flask(__name__)
app.register_blueprint(industry_decline_bp)

# 行业行情缓存（60秒），避免频繁请求同花顺
_industry_cache = None
_industry_cache_ts = 0.0

# 板块指数走势缓存：name -> (ts, rows)，5分钟
_industry_hist_cache = {}
_industry_hist_ttl = 300

# 全市场高股息率筛选缓存（30分钟）
_screener_cache = None
_screener_cache_ts = 0.0
_SCREENER_TTL = 1800

_THS_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36"
}
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                code       TEXT PRIMARY KEY,
                name       TEXT,
                added_at   TEXT,
                interested INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS records (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                code    TEXT NOT NULL,
                date    TEXT NOT NULL,
                yield   REAL NOT NULL,
                price   REAL,
                source  TEXT DEFAULT 'auto',   -- auto | manual
                note    TEXT,
                UNIQUE(code, date)
            );
            """
        )
        # 兼容旧库：补上 interested 列
        try:
            conn.execute("ALTER TABLE stocks ADD COLUMN interested INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # 列已存在
        conn.commit()


# --------------------------------------------------------------------------
# akshare 抓取（带重试，应对东财偶发 RemoteDisconnected）
# --------------------------------------------------------------------------
def _retry(fn, tries=3, delay=1.5):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay * (i + 1))
    raise last


_name_cache = None
_name_cache_ts = 0.0


def get_name_map():
    """返回 {code: name}，缓存1小时。"""
    global _name_cache, _name_cache_ts
    now = time.time()
    if _name_cache is None or now - _name_cache_ts > 3600:
        df = _retry(lambda: ak.stock_info_a_code_name())
        _name_cache = dict(zip(df["code"].astype(str), df["name"]))
        _name_cache_ts = now
    return _name_cache


def get_dividends(code):
    """返回已实施分红的 DataFrame：除权除息日、派息(每10股)。"""
    df = _retry(lambda: ak.stock_history_dividend_detail(symbol=code, indicator="分红"))
    df["除权除息日"] = pd.to_datetime(df["除权除息日"], errors="coerce")
    df = df[(df["进度"] == "实施") & df["除权除息日"].notna()]
    return df.sort_values("除权除息日")


def get_price_history(code):
    """返回日线 DataFrame：数据日期、当日收盘价（按日期升序）。"""
    df = _retry(lambda: ak.stock_value_em(symbol=code))
    df["数据日期"] = pd.to_datetime(df["数据日期"], errors="coerce")
    df = df.dropna(subset=["数据日期", "当日收盘价"])
    return df.sort_values("数据日期")


def compute_yield_series(code, max_days=400):
    """计算最近 max_days 个交易日的股息率序列。

    返回 [{date, yield, price}, ...] 按日期升序。
    """
    prices = get_price_history(code)
    divs = get_dividends(code)
    div_list = [(d, float(a) / 10.0) for d, a in zip(divs["除权除息日"], divs["派息"])]
    out = []
    sub = prices.tail(max_days)
    for d, price in zip(sub["数据日期"], sub["当日收盘价"]):
        price = float(price)
        year = d.year
        # 当前年度所有已实施分红
        current_year_divs = [amt for dd, amt in div_list if dd.year == year]
        if current_year_divs:
            total_div = sum(current_year_divs)
        else:
            # 当前年度尚无分红，使用上一年度
            total_div = sum(amt for dd, amt in div_list if dd.year == year - 1)
        y = (total_div / price * 100.0) if price > 0 else 0.0
        out.append({"date": d.strftime("%Y-%m-%d"), "yield": round(y, 3), "price": round(price, 3)})
    return out


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stocks", methods=["GET"])
def list_stocks():
    with closing(get_db()) as conn:
        stocks = conn.execute("SELECT * FROM stocks ORDER BY added_at").fetchall()
        result = []
        for s in stocks:
            rec = conn.execute(
                "SELECT * FROM records WHERE code=? ORDER BY date DESC LIMIT 1", (s["code"],)
            ).fetchone()
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM records WHERE code=?", (s["code"],)
            ).fetchone()["c"]
            result.append(
                {
                    "code": s["code"],
                    "name": s["name"],
                    "added_at": s["added_at"],
                    "interested": bool(s["interested"]),
                    "latest_yield": rec["yield"] if rec else None,
                    "latest_date": rec["date"] if rec else None,
                    "latest_source": rec["source"] if rec else None,
                    "latest_price": rec["price"] if rec else None,
                    "count": cnt,
                }
            )
    return jsonify(result)


@app.route("/api/stocks", methods=["POST"])
def add_stock():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    interested = 1 if body.get("interested") else 0
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"error": "股票代码须为6位数字"}), 400

    nm = get_name_map()
    if code not in nm:
        return jsonify({"error": "未找到该A股代码，请确认后重试"}), 400
    name = nm[code]

    with closing(get_db()) as conn:
        try:
            conn.execute(
                "INSERT INTO stocks(code, name, added_at, interested) VALUES(?,?,?,?)",
                (code, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), interested),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 已存在：更新到所选分类
            conn.execute(
                "UPDATE stocks SET interested=?, name=? WHERE code=?", (interested, name, code)
            )
            conn.commit()

    # 回填近1年历史，让图表立即有数据
    backfilled = 0
    warning = None
    try:
        series = compute_yield_series(code, max_days=400)
        with closing(get_db()) as conn:
            for row in series:
                conn.execute(
                    "INSERT OR REPLACE INTO records(code, date, yield, price, source, note) "
                    "VALUES(?,?,?,?,?,?)",
                    (code, row["date"], row["yield"], row["price"], "auto", None),
                )
            conn.commit()
        backfilled = len(series)
    except Exception as e:  # noqa: BLE001
        warning = f"已添加，但历史回填失败：{e}"

    return jsonify({
        "code": code, "name": name, "interested": bool(interested),
        "backfilled": backfilled, "warning": warning,
    })


@app.route("/api/stocks/<code>", methods=["DELETE"])
def del_stock(code):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM stocks WHERE code=?", (code,))
        conn.execute("DELETE FROM records WHERE code=?", (code,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/stocks/<code>/interested", methods=["POST"])
def toggle_interested(code):
    """切换感兴趣标记。"""
    body = request.get_json(silent=True) or {}
    val = 1 if body.get("interested") else 0
    with closing(get_db()) as conn:
        conn.execute("UPDATE stocks SET interested=? WHERE code=?", (val, code))
        conn.commit()
    return jsonify({"ok": True, "code": code, "interested": bool(val)})


@app.route("/api/dividends/<code>", methods=["GET"])
def dividend_history(code):
    """返回历年分红明细，按年份分组。"""
    try:
        df = get_dividends(code)
    except Exception as e:
        return jsonify({"error": f"获取分红数据失败：{e}"}), 500

    if df.empty:
        return jsonify({"code": code, "years": []})

    years_data = {}
    for _, row in df.iterrows():
        d = row["除权除息日"]
        amt = float(row["派息"]) / 10.0  # 每10股 → 每股
        y = d.year
        if y not in years_data:
            years_data[y] = {"year": y, "total": 0.0, "records": []}
        years_data[y]["records"].append({
            "date": d.strftime("%Y-%m-%d"),
            "amount": round(amt, 4),
        })
        years_data[y]["total"] = round(years_data[y]["total"] + amt, 4)

    years_list = sorted(years_data.values(), key=lambda x: x["year"], reverse=True)
    return jsonify({"code": code, "years": years_list})


@app.route("/api/industries", methods=["GET"])
def industries():
    """A股细分行业实时涨跌（同花顺），按涨跌幅降序。缓存60秒。"""
    global _industry_cache, _industry_cache_ts
    now = time.time()
    if _industry_cache is None or now - _industry_cache_ts > 60:
        df = _retry(lambda: ak.stock_board_industry_summary_ths())
        _industry_cache = df
        _industry_cache_ts = now

    rows = []
    for _, r in _industry_cache.iterrows():
        rows.append(
            {
                "name": str(r["板块"]),
                "pct": float(r["涨跌幅"]) if pd.notna(r["涨跌幅"]) else 0.0,
                "amount": float(r["总成交额"]) if pd.notna(r["总成交额"]) else 0.0,
                "net_inflow": float(r["净流入"]) if pd.notna(r["净流入"]) else 0.0,
                "up": int(r["上涨家数"]) if pd.notna(r["上涨家数"]) else 0,
                "down": int(r["下跌家数"]) if pd.notna(r["下跌家数"]) else 0,
                "leader": str(r["领涨股"]),
                "leader_pct": float(r["领涨股-涨跌幅"]) if pd.notna(r["领涨股-涨跌幅"]) else 0.0,
            }
        )
    rows.sort(key=lambda x: x["pct"], reverse=True)
    return jsonify(
        {
            "updated": datetime.now().strftime("%H:%M:%S"),
            "count": len(rows),
            "rows": rows,
        }
    )


@app.route("/api/industry/<name>", methods=["GET"])
def industry_history(name):
    """同花顺板块指数近一年走势（日线收盘价），缓存5分钟。"""
    cached = _industry_hist_cache.get(name)
    if cached and time.time() - cached[0] < _industry_hist_ttl:
        return jsonify({"name": name, "count": len(cached[1]), "rows": cached[1]})

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=3660)).strftime("%Y%m%d")  # 近10年
    df = _retry(lambda: ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end))
    df = df.dropna(subset=["日期", "收盘价"])

    rows = []
    prev = None
    for _, r in df.iterrows():
        close = float(r["收盘价"])
        pct = None if prev is None else (close / prev - 1.0) * 100.0
        rows.append({"date": str(r["日期"]), "close": round(close, 2), "pct": round(pct, 2) if pct is not None else None})
        prev = close

    _industry_hist_cache[name] = (time.time(), rows)
    return jsonify({"name": name, "count": len(rows), "rows": rows})


# --------------------------------------------------------------------------
# 全市场高股息率筛选（>3% 按行业分组）
# --------------------------------------------------------------------------
def _ths_board_members(board_code):
    """扫描同花顺单个细分行业的全部成分股，返回 {code: (名称, 现价)}。

    同花顺成分列表每页20只、默认最多返回100只；用「默认序 + 升序」两遍
    扫描取并集即可覆盖全部成分（对不足100只的板块只需一遍）。
    """
    s = requests.Session()
    s.trust_env = False  # 同花顺为国内站点，直连更稳（系统代理不稳定）
    s.headers.update(_THS_UA)
    members = {}
    for order in ("", "order/asc/"):
        page = 1
        while True:
            try:
                r = s.get(
                    f"http://q.10jqka.com.cn/thshy/detail/{order}page/{page}/code/{board_code}/",
                    timeout=10,
                )
                soup = BeautifulSoup(r.text, "lxml")
                table = soup.find("table", attrs={"class": "m-table"})
                rows = [tr for tr in (table.find_all("tr") if table else []) if tr.find("td")]
            except Exception:  # noqa: BLE001 单页失败即放弃该板块后续页
                break
            if not rows:
                break
            for tr in rows:
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) >= 4 and tds[1].isdigit():
                    members[tds[1]] = (tds[2], tds[3])  # code -> (名称, 现价)
            if len(rows) < 20 or len(members) >= 200:
                break
            page += 1
        if len(members) < 100:
            break  # 未触发100只封顶，另一序不会带来新成分
    return members


def _fetch_share_bonus(last_year, this_year):
    """分页抓东财「分红送配」报表（除权除息日在上一年起），
    返回 {code: {上一年: 每股合计, 当年: 每股合计}}（每10股派息/10=每股）。
    """
    agg = {}
    page = 1
    total = None
    while True:
        params = {
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
            "pageSize": "500",
            "pageNumber": str(page),
            "filter": f"(EX_DIVIDEND_DATE>='{last_year}-01-01')",
        }
        try:
            j = requests.get(
                _DATACENTER_URL,
                params=params,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/yjfp/"},
                timeout=20,
            ).json()
        except Exception:  # noqa: BLE001
            break
        res = j.get("result") or {}
        data = res.get("data") or []
        if not data:
            break
        for r in data:
            if r.get("ASSIGN_PROGRESS") != "实施分配":
                continue
            amt = r.get("PRETAX_BONUS_RMB")
            ex = str(r.get("EX_DIVIDEND_DATE"))[:10]
            if not amt or len(ex) != 10:
                continue
            code = r.get("SECURITY_CODE")
            try:
                year = int(ex[:4])
            except ValueError:
                continue
            if year not in (last_year, this_year):
                continue
            d = agg.setdefault(code, {last_year: 0.0, this_year: 0.0})
            d[year] += float(amt) / 10.0
        total = res.get("count")
        if total is None or page * 500 >= total:
            break
        page += 1
    return agg


def compute_screener():
    """全市场股息率>3% 按同花顺细分行业分组。

    股息率规则与个股卡片一致：优先当年（除权除息日所属年）全部已实施分红，
    当年无分红则用上一年度全部分红；现价取同花顺成分页最新价。
    """
    this_year = datetime.now().year
    last_year = this_year - 1

    boards = _retry(lambda: ak.stock_board_industry_name_ths())
    board_names = dict(zip(boards["code"].astype(str), boards["name"]))

    board_of_code, name_of_code, price_of_code = {}, {}, {}
    bcodes = list(board_names.keys())
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(_ths_board_members, bcodes))
    for bcode, members in zip(bcodes, results):
        bname = board_names[bcode]
        for code, (nm, pr) in members.items():
            board_of_code[code] = bname
            name_of_code[code] = nm
            try:
                price_of_code[code] = float(pr)
            except ValueError:
                pass  # 停牌等无现价

    divs = _fetch_share_bonus(last_year, this_year)

    groups = {}
    for code, years in divs.items():
        if code not in board_of_code:
            continue
        price = price_of_code.get(code)
        if not price or price <= 0:
            continue
        total = years[this_year] if years[this_year] > 0 else years[last_year]
        if total <= 0:
            continue
        y = total / price * 100.0
        if y <= 3.0:
            continue
        groups.setdefault(board_of_code[code], []).append(
            {
                "code": code,
                "name": name_of_code.get(code, code),
                "price": round(price, 2),
                "div": round(total, 4),
                "yield": round(y, 2),
            }
        )

    group_list = []
    for bname, lst in groups.items():
        lst.sort(key=lambda x: x["yield"], reverse=True)
        group_list.append(
            {"industry": bname, "count": len(lst), "max_yield": lst[0]["yield"], "stocks": lst}
        )
    group_list.sort(key=lambda x: x["max_yield"], reverse=True)

    return {
        "count": sum(len(g["stocks"]) for g in group_list),
        "industry_count": len(group_list),
        "groups": group_list,
    }


@app.route("/api/dividend-screener", methods=["GET"])
def dividend_screener():
    """全市场股息率>3%按行业分组。?refresh=1 强制刷新缓存。"""
    global _screener_cache, _screener_cache_ts
    refresh = request.args.get("refresh") == "1"
    now = time.time()
    if not refresh and _screener_cache is not None and now - _screener_cache_ts < _SCREENER_TTL:
        return jsonify(_screener_cache)
    try:
        payload = compute_screener()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"高股息筛选失败：{e}"}), 500
    payload["updated"] = datetime.now().strftime("%H:%M:%S")
    _screener_cache = payload
    _screener_cache_ts = time.time()
    return jsonify(payload)


@app.route("/api/fetch", methods=["POST"])
def fetch():
    """抓取最新交易日的股息率。body 可选 {code} 指定单只，否则抓取全部。"""
    body = request.get_json(silent=True) or {}
    code = body.get("code")
    with closing(get_db()) as conn:
        if code:
            rows = conn.execute("SELECT code FROM stocks WHERE code=?", (code,)).fetchall()
        else:
            rows = conn.execute("SELECT code FROM stocks ORDER BY added_at").fetchall()

    results = []
    for r in rows:
        c = r["code"]
        try:
            series = compute_yield_series(c, max_days=1)
            if not series:
                results.append({"code": c, "ok": False, "error": "无价格数据"})
                continue
            row = series[-1]
            with closing(get_db()) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO records(code, date, yield, price, source, note) "
                    "VALUES(?,?,?,?,?,?)",
                    (c, row["date"], row["yield"], row["price"], "auto", None),
                )
                conn.commit()
            results.append({"code": c, "ok": True, "date": row["date"], "yield": row["yield"]})
        except Exception as e:  # noqa: BLE001
            results.append({"code": c, "ok": False, "error": str(e)})
    return jsonify(results)


@app.route("/api/records/<code>", methods=["GET"])
def records(code):
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT date, yield, price, source, note FROM records WHERE code=? ORDER BY date",
            (code,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/records/<code>", methods=["POST"])
def upsert_record(code):
    body = request.get_json(silent=True) or {}
    d = str(body.get("date", "")).strip()
    y = body.get("yield")
    note = body.get("note")
    if not d or y is None:
        return jsonify({"error": "需要 date 和 yield"}), 400
    try:
        yf = float(y)
    except (TypeError, ValueError):
        return jsonify({"error": "股息率须为数字"}), 400
    with closing(get_db()) as conn:
        # 若该日已有自动记录，保留其 price；source 标记为 manual
        existing = conn.execute(
            "SELECT price FROM records WHERE code=? AND date=?", (code, d)
        ).fetchone()
        price = existing["price"] if existing else None
        conn.execute(
            "INSERT OR REPLACE INTO records(code, date, yield, price, source, note) "
            "VALUES(?,?,?,?,?,?)",
            (code, d, yf, price, "manual", note),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/records/<code>", methods=["DELETE"])
def del_record(code):
    body = request.get_json(silent=True) or {}
    d = body.get("date") or request.args.get("date")
    if not d:
        return jsonify({"error": "需要 date"}), 400
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM records WHERE code=? AND date=?", (code, d))
        conn.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
