# -*- coding: utf-8 -*-
"""生成 11 个前端页面（零构建静态多页，数据来自 output/api/v1 静态快照）。"""
import io
import os

PAGES = [
    ("dashboard", "投资驾驶舱"),
    ("market", "市场分析"),
    ("screener", "ETF 筛选"),
    ("etf", "ETF 详情"),
    ("strategy", "策略中心"),
    ("backtest", "回测中心"),
    ("portfolio", "组合管理"),
    ("risk", "风险管理"),
    ("decision", "交易决策"),
    ("review", "投资复盘"),
    ("settings", "系统设置"),
]

TPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} · Smart ETF Quant V5</title>
<link rel="stylesheet" href="assets/theme.css">
</head>
<body data-page="{key}">
<div id="app"></div>
<script src="assets/charts.js?v=6"></script>
<script src="assets/app.js?v=6"></script>
</body>
</html>
"""

INDEX = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Smart ETF Quant V5</title>
<meta http-equiv="refresh" content="0; url=dashboard.html">
<link rel="stylesheet" href="assets/theme.css"></head>
<body data-page="index"><div id="app" class="wrap" style="padding-top:40px">
<h1 style="font-size:18px">Smart ETF Quant V5.0</h1>
<p><a href="dashboard.html">进入投资驾驶舱</a></p>
<p class="mini">11 个页面：投资驾驶舱 / 市场分析 / ETF 筛选 / ETF 详情 / 策略中心 / 回测中心 / 组合管理 / 风险管理 / 交易决策 / 投资复盘 / 系统设置</p>
</div></body></html>
"""

here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
os.makedirs(here, exist_ok=True)
for key, title in PAGES:
    p = os.path.join(here, key + ".html")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(TPL.format(key=key, title=title))
with io.open(os.path.join(here, "index.html"), "w", encoding="utf-8") as f:
    f.write(INDEX)
print("written:", len(PAGES) + 1)
