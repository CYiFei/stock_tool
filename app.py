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
from datetime import datetime
from contextlib import closing

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")

app = Flask(__name__)


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
                code     TEXT PRIMARY KEY,
                name     TEXT,
                added_at TEXT
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
                    "latest_yield": rec["yield"] if rec else None,
                    "latest_date": rec["date"] if rec else None,
                    "latest_source": rec["source"] if rec else None,
                    "count": cnt,
                }
            )
    return jsonify(result)


@app.route("/api/stocks", methods=["POST"])
def add_stock():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    if not (code.isdigit() and len(code) == 6):
        return jsonify({"error": "股票代码须为6位数字"}), 400

    nm = get_name_map()
    if code not in nm:
        return jsonify({"error": "未找到该A股代码，请确认后重试"}), 400
    name = nm[code]

    with closing(get_db()) as conn:
        try:
            conn.execute(
                "INSERT INTO stocks(code, name, added_at) VALUES(?,?,?)",
                (code, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # 已存在，忽略

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

    return jsonify({"code": code, "name": name, "backfilled": backfilled, "warning": warning})


@app.route("/api/stocks/<code>", methods=["DELETE"])
def del_stock(code):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM stocks WHERE code=?", (code,))
        conn.execute("DELETE FROM records WHERE code=?", (code,))
        conn.commit()
    return jsonify({"ok": True})


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
