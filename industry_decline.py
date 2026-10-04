#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股三级行业下跌比例 —— Flask 蓝图。

统计同花顺 90 个三级行业（细分行业）全部成分股中，收盘价低于 1~10 年前的
股票占比。页面 /industry-decline，数据接口 /api/industry-decline*。

数据源（国内站点，均直连不走系统代理）：
  - 行业板块列表与成分股：同花顺 q.10jqka.com.cn/thshy/
  - 月K收盘价（后复权）与现价：腾讯 web.ifzq.gtimg.cn/appstock/app/fqkline/get

口径：
  - 基准价 = 个股最近一根月K收盘（后复权，含进行中的当月）；
  - 近 N 年 = 基准日减 N 个自然年，取该日之前最后一根月K收盘作比较；
  - 下跌比例 = 该行业内「近 N 年涨跌幅 < 0」的股票数 ÷ N 年前已有月K收盘的
    股票数（上市不足 N 年的个股不计入该列分母）；
  - 涨跌幅按后复权价计算，即已考虑历史分红送转（近似总回报）；腾讯前复权
    对高分红股票会算出负的历史价格，故涨跌幅不用前复权；
  - 成分股为当前名单，已退市个股不在统计内（幸存者偏差）。
"""

import os
import re
import sqlite3
import threading
import time
import traceback
from bisect import bisect_right
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Blueprint, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")

bp = Blueprint("industry_decline", __name__)

_THS_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36"
}
_KLINE_HOSTS = [
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
]
# 月K起点：10年比较窗再多留1年余量
_KLINE_START = f"{datetime.now().year - 11}-01-01"
_PRICE_WORKERS = 16
_HORIZONS = list(range(1, 11))  # 近1年 ~ 近10年


# --------------------------------------------------------------------------
# 限速与熔断（腾讯行情有 WAF，请求过密会被整站拦截，只能慢速抓取）
# --------------------------------------------------------------------------
_REQ_INTERVAL = 0.1  # 全局相邻请求最小间隔（秒），约 10 req/s
_WARMUP_N = 25       # 开局连续失败熔断阈值
_WINDOW_N = 100      # 滚动失败率熔断窗口
_WINDOW_MAX_FAIL = 60


class WafBlocked(Exception):
    """数据源返回 WAF 拦截页（501 挑战页等）。"""


class _RateLimiter:
    """跨线程共享的全局最小请求间隔。"""

    def __init__(self, interval):
        self.interval = interval
        self._next_t = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_t - now)
            self._next_t = max(self._next_t, now) + self.interval
        if wait > 0:
            time.sleep(wait)


_rate = _RateLimiter(_REQ_INTERVAL)
_waf_event = threading.Event()   # 主备域名均被拦截时置位：所有腾讯请求立即放弃
_host_lock = threading.Lock()
_preferred_host = 0              # 最近一次成功的域名下标（粘性优选）


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------
_schema_ready = False
_schema_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with closing(_conn()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS idc_members (
                    board TEXT NOT NULL,
                    code  TEXT NOT NULL,
                    name  TEXT,
                    price REAL,
                    PRIMARY KEY (board, code)
                );
                CREATE TABLE IF NOT EXISTS idc_month_close (
                    code  TEXT NOT NULL,
                    date  TEXT NOT NULL,   -- 月K交易日 YYYY-MM-DD
                    close REAL NOT NULL,
                    PRIMARY KEY (code, date)
                );
                CREATE TABLE IF NOT EXISTS idc_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()
        _schema_ready = True


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM idc_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO idc_meta(key, value) VALUES(?,?)", (key, str(value))
    )


# --------------------------------------------------------------------------
# 抓取：同花顺行业与成分股
# --------------------------------------------------------------------------
def _direct_session():
    """国内数据源直连。系统代理（http_proxy 环境变量）对这些站点不稳定。"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update(_THS_UA)
    return s


def _retry_get(sess, url, params=None, tries=4, tencent=False):
    last = None
    for i in range(tries):
        if _waf_event.is_set():
            raise WafBlocked("已熔断")
        _rate.acquire()
        try:
            r = sess.get(url, params=params, timeout=10)
            if tencent and (
                r.status_code in (403, 429, 501) or "waf.tencent.com" in r.text[:600]
            ):
                raise WafBlocked(f"腾讯WAF拦截 HTTP {r.status_code}")
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except WafBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.4 * (i + 1))
    raise last


def _scan_boards():
    """同花顺行业板块列表 [(code, name)]，来自 thshy 页面内嵌的板块链接。"""
    sess = _direct_session()
    r = _retry_get(sess, "http://q.10jqka.com.cn/thshy/")
    pairs = re.findall(
        r'href="http://q\.10jqka\.com\.cn/thshy/detail/code/(\d+)/"[^>]*>([^<]{1,12})</a>',
        r.text,
    )
    boards = {}
    for code, name in pairs:
        if code.startswith("88"):
            boards[code] = name.strip()
    if len(boards) < 50:
        raise RuntimeError(f"同花顺行业列表解析异常，仅得到 {len(boards)} 个板块")
    return sorted(boards.items())


def _scan_board_members(board_code):
    """扫描单个行业板块全部成分股，返回 {code: name}。

    成分列表每页20只、默认最多返回100只；「默认序 + 升序」两遍扫描取并集
    即可覆盖全部成分（不足100只的板块一遍即完成）。
    """
    sess = _direct_session()
    members = {}
    for order in ("", "order/asc/"):
        page = 1
        while True:
            try:
                r = _retry_get(
                    sess,
                    f"http://q.10jqka.com.cn/thshy/detail/{order}page/{page}/code/{board_code}/",
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
                if len(tds) >= 3 and tds[1].isdigit():
                    members[tds[1]] = tds[2]
            if len(rows) < 20 or len(members) >= 200:
                break
            page += 1
        if len(members) < 100:
            break
    return members


# --------------------------------------------------------------------------
# 抓取：腾讯月K（前复权）
# --------------------------------------------------------------------------
def _tx_symbol(code):
    if code.startswith(("60", "68", "90")):
        return "sh" + code
    if code.startswith(("00", "30", "20")):
        return "sz" + code
    if code.startswith(("43", "83", "87", "92")):
        return "bj" + code
    return None


def _fetch_kline(sess, sym):
    """按粘性优选顺序尝试主备域名；两个域都被 WAF 拦截时置全局熔断并抛出。"""
    global _preferred_host
    with _host_lock:
        order = [_preferred_host] + [i for i in range(len(_KLINE_HOSTS)) if i != _preferred_host]
    last = None
    for idx in order:
        try:
            r = _retry_get(sess, _KLINE_HOSTS[idx], params={
                "param": f"{sym},month,{_KLINE_START},2100-01-01,320,hfq"
            }, tries=2, tencent=True)
            with _host_lock:
                _preferred_host = idx
            return r
        except WafBlocked as e:
            if _waf_event.is_set():
                raise
            last = e
            continue
    _waf_event.set()
    raise last


def _fetch_stock(code):
    """返回 ([(date, hfq_close), ...], 现价)；失败返回 ([], None)。

    月K用后复权（前复权对高分红股会出现负的历史价，不能用于算涨跌幅）；
    现价取响应内嵌 qt 实时行情；主域名被 WAF 拦截时自动切换备用域名。
    """
    sym = _tx_symbol(code)
    if not sym:
        return [], None
    sess = _direct_session()
    for i in range(3):
        if _waf_event.is_set():
            return [], None
        try:
            r = _fetch_kline(sess, sym)
            d = r.json().get("data", {}).get(sym) or {}
            k = d.get("hfqmonth") or d.get("month") or []
            bars = []
            for b in k:
                if len(b) >= 3 and re.match(r"^\d{4}-\d{2}-\d{2}$", str(b[0])):
                    try:
                        bars.append((str(b[0]), float(b[2])))
                    except (TypeError, ValueError):
                        continue
            bars.sort(key=lambda x: x[0])
            price = None
            qt = (d.get("qt") or {}).get(sym)
            if qt and len(qt) > 3:
                try:
                    p = float(qt[3])
                    price = p if p > 0 else None
                except (TypeError, ValueError):
                    pass
            return bars, price
        except WafBlocked:
            return [], None
        except Exception:  # noqa: BLE001
            time.sleep(0.5 * (i + 1))
    return [], None


# --------------------------------------------------------------------------
# 构建（后台线程）
# --------------------------------------------------------------------------
_build_state = {
    "status": "idle",       # idle | building | ready | error
    "phase": "",
    "done": 0,
    "total": 0,
    "failed": 0,
    "started_at": None,
    "built_at": None,
    "error": None,
}
_state_lock = threading.Lock()
_build_lock = threading.Lock()      # 同一时间只允许一个构建
_payload_cache = None
_cache_lock = threading.Lock()


def _set_state(**kw):
    with _state_lock:
        _build_state.update(kw)


def _get_state():
    with _state_lock:
        return dict(_build_state)


def _start_build():
    """启动后台构建；已有构建进行中时返回 False。"""
    with _build_lock:
        st = _get_state()
        if st["status"] == "building":
            return False
        _waf_event.clear()
        _set_state(
            status="building", phase="准备", done=0, total=0, failed=0,
            error=None, started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        t = threading.Thread(target=_build_all, daemon=True)
        t.start()
        return True


def _build_all():
    _ensure_schema()
    try:
        # 1) 行业与成分股
        _set_state(phase="扫描行业成分股")
        boards = _scan_boards()
        board_names = dict(boards)
        members = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_scan_board_members, bc): bc for bc, _ in boards}
            for fu in as_completed(futs):
                members[futs[fu]] = fu.result()

        board_of, name_of = {}, {}
        bj_excluded = 0
        with closing(_conn()) as conn:
            conn.execute("DELETE FROM idc_members")
            for bc, mem in members.items():
                for code, nm in mem.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO idc_members(board, code, name, price) VALUES(?,?,?,NULL)",
                        (board_names[bc], code, nm),
                    )
                    if code.startswith("92"):
                        continue  # 北交所：数据源无月K，不计入统计
                    board_of.setdefault(code, board_names[bc])
                    name_of.setdefault(code, nm)
            conn.commit()

        codes = sorted(board_of)
        bj_excluded = sum(1 for mem in members.values() for c in mem if c.startswith("92"))
        total = len(codes)
        _set_state(phase="抓取个股月K收盘价（限速约10条/秒）", done=0, total=total)
        # 旧数据保留（构建失败时页面仍可用旧数据）；仅价格口径变更时全量重刷
        with closing(_conn()) as conn:
            if _meta_get(conn, "price_kind") != "hfq":
                conn.execute("DELETE FROM idc_month_close")
                _meta_set(conn, "price_kind", "hfq")
            _meta_set(conn, "data_complete", "0")
            conn.commit()

        # 2) 逐股抓月K，分批落库；开局与滚动失败率熔断，防止源站限流时空转
        buffer = []
        price_buffer = []
        failed = 0
        done = 0
        window = deque(maxlen=_WINDOW_N)
        abort_reason = None

        def flush():
            nonlocal buffer, price_buffer
            if buffer:
                with closing(_conn()) as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO idc_month_close(code, date, close) VALUES(?,?,?)",
                        buffer,
                    )
                    conn.executemany(
                        "UPDATE idc_members SET price=? WHERE board=? AND code=?",
                        price_buffer,
                    )
                    conn.commit()
                buffer = []
                price_buffer = []

        with ThreadPoolExecutor(max_workers=_PRICE_WORKERS) as ex:
            futs = {ex.submit(_fetch_stock, c): c for c in codes}
            for fu in as_completed(futs):
                code = futs[fu]
                done += 1
                try:
                    bars, price = fu.result()
                except Exception:  # noqa: BLE001
                    bars, price = [], None
                ok = bool(bars)
                window.append(ok)
                if not ok:
                    failed += 1
                    if done == _WARMUP_N and failed == done:
                        abort_reason = (
                            f"开局 {_WARMUP_N} 只个股全部抓取失败——数据源被限流(WAF)或不可达，构建中止"
                        )
                        _waf_event.set()
                        break
                    if len(window) == _WINDOW_N and \
                            sum(1 for x in window if not x) > _WINDOW_MAX_FAIL:
                        abort_reason = "抓取失败率过高（数据源疑似限流），构建中止，请稍后重试"
                        _waf_event.set()
                        break
                buffer.extend((code, d, c) for d, c in bars)
                if price is not None:
                    price_buffer.append((price, board_of[code], code))
                if done % 50 == 0:
                    flush()
                if done % 10 == 0 or done == total:
                    _set_state(done=done, failed=failed)
        flush()
        if abort_reason or _waf_event.is_set():
            raise RuntimeError(abort_reason or "数据源被限流(WAF)，构建中止，请稍后重试")

        # 3) 计算并缓存
        _set_state(phase="计算下跌比例")
        payload = _compute_payload()
        payload["excluded_bse"] = bj_excluded
        with closing(_conn()) as conn:
            _meta_set(conn, "data_complete", "1")
            _meta_set(conn, "built_at", _build_state["started_at"])
            _meta_set(conn, "anchor", payload["anchor"])
            _meta_set(conn, "board_count", len(payload["boards"]))
            _meta_set(conn, "stock_count", payload["market"]["total"])
            _meta_set(conn, "failed_count", failed)
            _meta_set(conn, "excluded_bse", bj_excluded)
            # 清理已不在任何行业名单里的历史个股
            conn.execute(
                "DELETE FROM idc_month_close WHERE code NOT IN (SELECT DISTINCT code FROM idc_members)"
            )
            conn.commit()
        global _payload_cache
        with _cache_lock:
            _payload_cache = payload
        _set_state(status="ready", phase="", built_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set_state(status="error", phase="", error=str(e))


# --------------------------------------------------------------------------
# 计算
# --------------------------------------------------------------------------
def _year_shift(date_str, n):
    """日期字符串减 n 个自然年（2/29 自动落到 2/28）。"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    try:
        return d.replace(year=d.year - n).strftime("%Y-%m-%d")
    except ValueError:
        return d.replace(year=d.year - n, month=2, day=28).strftime("%Y-%m-%d")


def _compute_payload():
    """由 idc_members + idc_month_close 计算全量 payload。"""
    closes_by_code = {}
    anchor = None
    with closing(_conn()) as conn:
        board_rows = conn.execute(
            "SELECT board, code, name, price FROM idc_members ORDER BY board, code"
        ).fetchall()
        for r in conn.execute(
            "SELECT code, date, close FROM idc_month_close ORDER BY code, date"
        ):
            lst = closes_by_code.setdefault(r["code"], [])
            lst.append((r["date"], r["close"]))
            if anchor is None or r["date"] > anchor:
                anchor = r["date"]

    if anchor is None:
        raise RuntimeError("无月K数据，无法计算")

    targets = {n: _year_shift(anchor, n) for n in _HORIZONS}

    boards = {}
    market_total = 0
    agg_market = {n: [0, 0] for n in _HORIZONS}  # n -> [down, comparable]

    for r in board_rows:
        boards.setdefault(r["board"], []).append(r)

    board_payload = []
    for bname in sorted(boards):
        stocks = []
        agg = {n: [0, 0] for n in _HORIZONS}
        for r in boards[bname]:
            code, sname, disp_price = r["code"], r["name"] or "", r["price"]
            bars = closes_by_code.get(code)
            if not bars:
                continue
            dates = [b[0] for b in bars]
            base = bars[-1][1]
            chg = []
            for n in _HORIZONS:
                idx = bisect_right(dates, targets[n]) - 1
                if idx < 0:
                    chg.append(None)
                    continue
                v = (base / bars[idx][1] - 1.0) * 100.0
                chg.append(round(v, 2))
                agg[n][1] += 1
                agg_market[n][1] += 1
                if v < 0:
                    agg[n][0] += 1
                    agg_market[n][0] += 1
            stocks.append({"code": code, "name": sname, "price": disp_price, "chg": chg})
        market_total += len(stocks)
        ratios = []
        for n in _HORIZONS:
            down, comp = agg[n]
            ratios.append({
                "n": n,
                "down": down,
                "comp": comp,
                "pct": round(down / comp * 100.0, 1) if comp else None,
            })
        # 个股默认按近1年涨跌幅升序（跌幅大的在前），无数据的排后面
        stocks.sort(key=lambda s: (s["chg"][0] is None, s["chg"][0] if s["chg"][0] is not None else 0))
        board_payload.append({"name": bname, "total": len(stocks), "ratios": ratios, "stocks": stocks})

    market_ratios = []
    for n in _HORIZONS:
        down, comp = agg_market[n]
        market_ratios.append({
            "n": n,
            "down": down,
            "comp": comp,
            "pct": round(down / comp * 100.0, 1) if comp else None,
        })

    return {
        "status": "ready",
        "anchor": anchor,
        "built_at": _build_state.get("built_at"),
        "horizons": _HORIZONS,
        "targets": {str(n): targets[n] for n in _HORIZONS},
        "market": {"name": "全市场", "total": market_total, "ratios": market_ratios},
        "boards": board_payload,
    }


def _payload_or_none():
    """内存缓存 → 库内 meta（重启后懒重建）。"""
    global _payload_cache
    _ensure_schema()
    with _cache_lock:
        if _payload_cache is not None:
            return _payload_cache
    with closing(_conn()) as conn:
        complete = _meta_get(conn, "data_complete")
    if complete != "1":
        return None
    try:
        payload = _compute_payload()
        with closing(_conn()) as conn:
            payload["excluded_bse"] = int(_meta_get(conn, "excluded_bse", 0) or 0)
    except Exception:  # noqa: BLE001
        return None
    with _cache_lock:
        _payload_cache = payload
    return payload


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@bp.route("/industry-decline")
def page():
    return render_template("industry_decline.html")


@bp.route("/api/industry-decline", methods=["GET"])
def api_payload():
    payload = _payload_or_none()
    if payload is not None:
        return jsonify(payload)
    # 尚无数据：自动触发首次构建
    st = _get_state()
    if st["status"] != "building":
        _start_build()
        st = _get_state()
    return jsonify({"status": "building", "progress": _progress_view(st)}), 202


@bp.route("/api/industry-decline/status", methods=["GET"])
def api_status():
    st = _get_state()
    out = {"status": st["status"], "progress": _progress_view(st)}
    if st["status"] == "error":
        out["error"] = st["error"]
    return jsonify(out)


@bp.route("/api/industry-decline/refresh", methods=["POST"])
def api_refresh():
    if not _start_build():
        return jsonify({"status": "building", "progress": _progress_view(_get_state())}), 202
    return jsonify({"status": "building", "progress": _progress_view(_get_state())}), 202


def _progress_view(st):
    return {
        "phase": st["phase"],
        "done": st["done"],
        "total": st["total"],
        "failed": st["failed"],
        "started_at": st["started_at"],
    }
