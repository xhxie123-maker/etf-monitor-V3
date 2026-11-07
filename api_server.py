# -*- coding: utf-8 -*-
"""
本地可选 REST 服务（标准库实现，零第三方依赖）
==============================================
GitHub Pages 只能托管静态文件，因此线上走 api_layer 的静态 JSON 快照；
本文件给「本地/内网调试」提供同一套 13 个端点的实时服务：

  python api_server.py --port 8010          # GET 读 output/api/v1 快照
  python api_server.py --port 8010 --live   # POST 会真实写库/跑回测

不使用 FastAPI（本机与 CI 均未安装，引入不可运行的依赖没有意义）；
路径、信封字段与 api_layer.ENDPOINTS 完全一致。
"""
from __future__ import annotations

import json
import os
import sys

MODEL_VERSION = "api-server-v5-2026-11-07"
ROOT = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(ROOT, "output", "api", "v1")


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class Router:
    """把 HTTP 路径映射到快照文件 / 写操作。"""

    def __init__(self, api_dir=API_DIR, live=False):
        self.api_dir = api_dir
        self.live = live

    def get(self, path):
        """返回 (status, payload)。"""
        p = path.split("?")[0]
        m = {
            "/api/v1/market/status": "market_status.json",
            "/api/v1/etfs": "etfs.json",
            "/api/v1/etfs/{code}/signals": "etf_signals.json",
            "/api/v1/etfs/{code}": "etf_detail.json",
            "/api/v1/rankings": "rankings.json",
            "/api/v1/portfolio": "portfolio.json",
            "/api/v1/portfolio/risk": "portfolio_risk.json",
            "/api/v1/recommendations": "recommendations.json",
            "/api/v1/backtests": "backtests.json",
            "/api/v1/data/health": "data_health.json",
        }
        if p == "/api/v1" or p == "/api/v1/":
            f = "index.json"
        elif p in m:
            f = m[p]
        elif p.startswith("/api/v1/etfs/"):
            f = "etf_detail.json"
        else:
            return 404, {"error": "not_found", "path": p,
                         "hint": "见 GET /api/v1（索引）列出的 13 个端点"}
        fp = os.path.join(self.api_dir, f)
        if not os.path.exists(fp):
            return 503, {"error": "snapshot_missing", "path": f,
                         "hint": "先运行 python etf_monitor.py --cloud 生成 output/api/v1 快照"}
        data = _read_json(fp)
        # 明细端点支持按 code 过滤
        if p.startswith("/api/v1/etfs/") and "/signals" not in p and p != "/api/v1/etfs/":
            code = p.rstrip("/").split("/")[-1]
            items = (data.get("data") or {}).get("items") or {}
            if code not in items:
                return 404, {"error": "code_not_found", "code": code}
            return 200, {"data_date": data.get("data_date"), "updated_at": data.get("updated_at"),
                         "sources": data.get("sources"), "model_versions": data.get("model_versions"),
                         "api_version": data.get("api_version"), "data": items[code]}
        if "/signals" in p:
            code = p.split("/api/v1/etfs/")[1].split("/")[0]
            items = (data.get("data") or {}).get("items") or {}
            return 200, {"data_date": data.get("data_date"), "updated_at": data.get("updated_at"),
                         "sources": data.get("sources"), "model_versions": data.get("model_versions"),
                         "api_version": data.get("api_version"),
                         "data": {"code": code, "signals": items.get(code) or []}}
        return 200, data

    # ------------------------------------------------------------ 写操作
    def post(self, path, body):
        p = path.split("?")[0]
        if p == "/api/v1/portfolio/positions":
            return self._positions(body)
        if p.startswith("/api/v1/recommendations/") and p.endswith("/review"):
            rid = p.split("/api/v1/recommendations/")[1].rsplit("/", 1)[0]
            return self._review(rid, body)
        if p == "/api/v1/backtests/run":
            return self._backtest(body)
        return 404, {"error": "not_found", "path": p}

    def _positions(self, body):
        holdings = (body or {}).get("holdings") or []
        if not isinstance(holdings, list) or not holdings:
            return 400, {"error": "bad_request", "hint": "holdings 需为非空数组"}
        bad = [h for h in holdings if not h.get("etf_code") or h.get("qty") is None]
        if bad:
            return 400, {"error": "bad_holdings", "items": bad}
        fp = os.path.join(ROOT, "output", "portfolio_positions.json")
        ledger = {"portfolio_id": body.get("portfolio_id") or "P_DEFAULT",
                  "cash_cny": body.get("cash_cny"), "holdings": holdings,
                  "source": "manual_api", "live": self.live}
        if not self.live:
            return 200, {"ok": False, "dry_run": True,
                         "note": "未启用 --live，仅校验入参未落盘", "echo": ledger}
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2, default=str)
        return 200, {"ok": True, "written": "output/portfolio_positions.json", "n": len(holdings)}

    def _review(self, rid, body):
        status = (body or {}).get("status")
        if status not in ("approved", "rejected", "expired"):
            return 400, {"error": "bad_status", "allowed": ["approved", "rejected", "expired"]}
        if not self.live:
            return 200, {"ok": False, "dry_run": True, "echo": {"recommendation_id": rid, "status": status}}
        try:
            import decision_center as dcx
            import smart_db as sdb
            conn = sdb.connect(os.path.join(ROOT, "output", "smart_etf.db"))
            ok = dcx.record_review(conn, rid, status, (body or {}).get("note"))
            conn.commit()
            conn.close()
            return 200, {"ok": bool(ok), "recommendation_id": rid, "status": status}
        except Exception as e:
            return 500, {"error": "write_failed", "detail": f"{type(e).__name__}: {e}"}

    def _backtest(self, body):
        code = (body or {}).get("code") or "510300"
        csv = os.path.join(ROOT, "output", f"price_history_{code}.csv")
        if not os.path.exists(csv):
            return 404, {"error": "no_price_file", "code": code}
        try:
            import backtest as bt
            import pandas as pd
            df = pd.read_csv(csv, parse_dates=["date"])[["date", "close"]]
            r = bt.run_backtest(df, fee=float((body or {}).get("fee", 0.001)),
                                slippage=float((body or {}).get("slippage", 0.001)),
                                deep=bool((body or {}).get("deep", False)))
            return 200, {"ok": r.get("ok"), "code": code, "result": r}
        except Exception as e:
            return 500, {"error": "backtest_failed", "detail": f"{type(e).__name__}: {e}"}


def serve(port=8010, live=False, api_dir=API_DIR):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    router = Router(api_dir=api_dir, live=live)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            raw = _json_bytes(obj)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            code, obj = router.get(self.path)
            self._send(code, obj)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                self._send(400, {"error": "bad_json"})
                return
            code, obj = router.post(self.path, body)
            self._send(code, obj)

        def log_message(self, fmt, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return srv


def self_test():
    """起临时端口跑一遍 13 端点的真实 HTTP 往返（不写库）。"""
    import threading
    import urllib.request
    res = []

    def chk(n, c, e=""):
        res.append({"name": n, "pass": bool(c), "extra": e})

    srv = serve(port=0, live=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def get(p):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def post(p, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{p}",
                                     data=_json_bytes(body), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    try:
        s, idx = get("/api/v1")
        have = os.path.exists(os.path.join(API_DIR, "etfs.json"))
        chk("索引端点返回 200", s == 200, str(s))
        chk("索引列出 13 个端点", (idx.get("count") == 13) if "count" in idx else True, str(idx.get("count")))
        s1, b1 = get("/api/v1/market/status")
        chk("GET market/status", s1 in (200, 503), str(s1))
        if have:
            s2, b2 = get("/api/v1/etfs")
            chk("GET etfs 有数据", s2 == 200 and (b2.get("data") or {}).get("count", 0) > 0,
                str((b2.get("data") or {}).get("count")))
            s3, b3 = get("/api/v1/etfs/510300")
            chk("GET etfs/510300 过滤生效", s3 == 200 and (b3.get("data") or {}).get("code") == "510300",
                str(s3))
            s4, b4 = get("/api/v1/etfs/999999")
            chk("未知代码返回 404", s4 == 404, str(s4))
        s5, b5 = get("/api/v1/nope")
        chk("未知路径返回 404", s5 == 404, str(s5))
        s6, b6 = post("/api/v1/portfolio/positions", {"holdings": [{"etf_code": "510300", "qty": 100}],
                                                     "cash_cny": 0})
        chk("POST positions 校验通过(dry_run)", s6 == 200 and b6.get("dry_run") is True, str(s6))
        s7, b7 = post("/api/v1/portfolio/positions", {"holdings": []})
        chk("POST positions 空持仓 400", s7 == 400, str(s7))
        s8, b8 = post("/api/v1/recommendations/X/review", {"status": "bad"})
        chk("POST review 非法状态 400", s8 == 400, str(s8))
        s9, b9 = post("/api/v1/recommendations/X/review", {"status": "approved"})
        chk("POST review 合法状态 dry_run", s9 == 200 and b9.get("dry_run") is True, str(s9))
        s10, b10 = post("/api/v1/backtests/run", {"code": "510300"})
        chk("POST backtests/run 返回指标", s10 == 200 and "result" in b10, str(s10))
    finally:
        srv.shutdown()
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--live", action="store_true", help="允许 POST 真实写库")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = self_test()
        for x in r:
            print(("PASS " if x["pass"] else "FAIL ") + x["name"] + (" | " + x["extra"] if x["extra"] else ""))
        print("%d/%d PASS" % (sum(1 for x in r if x["pass"]), len(r)))
        raise SystemExit(0 if all(x["pass"] for x in r) else 1)
    srv = serve(port=a.port, live=a.live)
    print(f"api server on http://127.0.0.1:{a.port}/api/v1  live={a.live}")
    srv.serve_forever()
