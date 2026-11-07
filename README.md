# Smart ETF Quant V5.0（三层定位 · 六层架构 · 四策略 · 五维评分 · 四级风控 · 决策闭环）

> 本系统按《构建量化交易工具手册》PRD 重构：在 V4.1（单文件内联看板）基础上做**增量式分层**改造，把行情采集、指标计算、评分、策略、风控、决策、复盘拆成**可独立单测、可版本追溯、可落库**的 L1–L6 六层。
> 配置唯一来源仍是 `portfolio_config.json`（`version: V5.0`，`strategy_version: v5-full-2026-11-07`）。
> 版本口径：各模块 `MODEL_VERSION` 统一带日期戳 `2026-11-07`，便于回放与审计。

**本系统为量化辅助决策工具，不连接券商、不自动下单、不做收益预测；所有输出均为辅助信息，最终交易由人工确认。仅供参考，不构成投资建议。**

---

## 1. 三层定位与六层架构

### 1.1 三层定位

| 层 | 内容 | 本系统落点 |
|---|---|---|
| 基础层 | 行情、估值、指标、规则、数据库 | `data_sources.py` / `smart_db.py` / `trading_rules.py` / `etf_monitor.py`（采集） |
| 智能层 | 五维评分、四策略、市场状态、压力测试 | `factor_engine.py` / `strategies.py` / `regime_engine.py` / `stress.py` |
| 应用层 | 仓位计划、决策清单、复盘、看板与 API | `position_sizing.py` / `risk_control.py` / `decision_center.py` / `v5_layer.py` / `api_layer.py` / `api_server.py` / `frontend/` |

### 1.2 六层架构（L1–L6）

| 层 | 职责 | 模块（真实文件） | 关键契约 |
|---|---|---|---|
| L1 数据层 | 采集、分级、落库、规则 | `data_sources.py`、`smart_db.py`、`trading_rules.py` | P0/P1/P2 分级；20 表 SQLite；`Q=floor(B/(P×L))×L` |
| L2 指标层 | 均线/动量/波动/回撤/RSI 逐日 | `etf_monitor.py`（当日）、`backfill_indicators.py`（历史回填） | `indicator_daily` 已回填 19293 行 |
| L3 评分与策略层 | 五维评分 + 四策略 + 市场状态 | `factor_engine.py`、`strategies.py`、`regime_engine.py` | 权重 25/20/15/15/25；A/B/C/D；三档 ×0.6~1.0 |
| L4 风险层 | 四级风控 + 熔断 + 六情景压力 | `risk_control.py`、`stress.py` | 波动/回撤/相关性/集中度；green/yellow/orange/red |
| L5 决策层 | 五闸门 + 12 字段建议 + 失效条件 + 复盘 | `position_sizing.py`、`decision_center.py` | 未确认风险偏好 → 仅观察（PRD 15.4） |
| L6 呈现层 | 12 区块看板 + 13 API + 11 页面 | `v5_layer.py`、`api_layer.py`、`api_server.py`、`frontend/` | 静态 JSON 快照 + 标准库实时服务 |

**层间约束（已在代码中强制）**：上层只读下层结构化输出，不反向依赖；每层都有 `MODEL_VERSION`；任一层异常在上层降级为「数据异常，暂停决策」（PRD 15.5），不产出默认信号。

---

## 2. 目录与文件说明

| 文件 | 作用 |
|---|---|
| `etf_monitor.py` | 主流程：抓数据 → 指标 → V4.1 卡片 → `v5_layer.build()` → 渲染 12 区块看板 |
| `v5_layer.py` | L1–L6 编排器：组装 payload、落库、写 API 快照、渲染 HTML 区块 |
| `factor_engine.py` | 五维评分（趋势/动量/估值/流动性/风险）+ 五级信号 + 横截面排名 |
| `strategies.py` | 策略 A 质量筛选 / B 趋势 / C 多周期动量 / D 稳健多资产 + `evaluate_all` |
| `regime_engine.py` | 市场三档状态（趋势 40% + 广度 30% + 波动 30%）→ `risk_multiplier` |
| `stress.py` | 六情景极端损失 + 历史最差窗口 + 蒙特卡洛（normal/t/bootstrap）+ 相关性突升 |
| `risk_control.py` | 四级风控：单只检查、组合检查、风险等级、四级熔断、VaR/CVaR/相关性 |
| `position_sizing.py` | 风险预算权重（`w ∝ base_w/σ`）+ 整手计划 + 约束检查 |
| `decision_center.py` | 五闸门、12 字段建议、失效条件、复盘状态机、下一交易日 |
| `trading_rules.py` | 品种规则层（T+0/T+1、盘后固定价格、最小单位） |
| `smart_db.py` | 20 表 SQLite schema + 17 个 sync + `counts`（全 20 表）/ `data_health` |
| `backfill_indicators.py` | `indicator_daily` 历史逐日回填（无未来函数） |
| `api_layer.py` | 13 个 REST 端点契约 + 静态 JSON 快照生成 |
| `api_server.py` | 标准库 `http.server` 实时服务（与快照同一响应契约） |
| `backtest.py` | 回测：成本/滑点/再平衡/九指标 + 防前视 + 滚动样本外 + 参数敏感性 |
| `frontend/` | 11 个零构建静态页面 + 索引页（驾驶舱/市场/筛选/详情/策略/回测/组合/风险/决策/复盘/设置） |
| `.github/workflows/daily.yml` | 每个工作日 15:35 自动运行 → 提交产物（含 `output/api`）→ 发布 Pages（含 `frontend/`） |

---

## 3. 数据库（SQLite，20 张业务表）

> PRD 原文为 PostgreSQL；实跑环境是 GitHub Pages（纯静态）+ Actions（无常驻进程），故改用 **SQLite**（同关系模型同 SQL、单文件零运维）。金额一律「分」整数（`*_cents`）。

| 表 | 行数（2026-11-07 实跑） | 说明 |
|---|---|---|
| `etf_basic` | 10 | 标的静态信息与资产类别 |
| `etf_daily_bar` | 19483 | 日线 OHLC（仅 close 可得，见限制） |
| `etf_valuation` | 10 | 估值（官方 PE 分位） |
| `indicator_daily` | 19293 | 逐日指标（MA/动量/波动/回撤/RSI） |
| `etf_signal` | 10 | 评分与信号快照 |
| `market_regime` | 1 | 市场状态（含 `risk_multiplier`） |
| `risk_snapshot` | 1 | 组合风险与熔断 |
| `recommendation` | 10 | 决策建议（12 字段 + `review_status`） |
| `backtest_run` | 2 | 回测结果（指标 + 防作弊结论） |
| `data_ingestion_log` | 36 | 每源每类一行（ok / degraded_p2 / failed） |
| `instrument_rule` | 10 | 品种规则（当前全部 `verified=False`） |
| `etf_profile_snapshot` | 10 | 产品快照（只写实际可得字段，不估算） |
| `portfolio` | 1 | 组合主记录（资金未确认，金额字段留 NULL） |
| `portfolio_daily` | 130 | 假设组合逐日净值（`quality_flag=hypothetical`） |
| `strategy_definition` | 4 | A/B/C/D 策略规则定义（幂等 upsert） |
| `strategy_version` | 4 | 策略版本（参数 JSON + 哈希 + 训练/验证区间） |
| `trading_calendar` | 5276 | 交易日历（从行情日期推导，无行情日 `is_open=0`） |
| `etf_flow` | 0 | 无可靠「份额变化 vs 成交额」分离数据源，如实留空，不造数 |
| `portfolio_position` | 0 | 用户未提供实际持仓；待人工输入后写入 |
| `trade_ledger` | 0 | PRD 规定只记用户实际确认的交易，无交易则为空 |

`portfolio_daily.quality_flag` 区分 `actual`（实盘）/ `hypothetical`（假设回放）；旧库缺列时 `init_db` 自动 `ALTER TABLE ADD COLUMN` 迁移。

健康检查：`smart_db.data_health()` 返回 `{ok, counts, last_bar_date, last_signal_date, failed_ingestions}`；`failed_ingestions > 0` 时 `ok=False`，属设计行为（P2 源失败会被如实记账）。

---

## 4. 五维评分（`factor_engine.py`）

- 权重：趋势 0.25 / 动量 0.20 / 估值 0.15 / 流动性 0.15 / 风险 0.25（`WEIGHTS`，和为 1，有单测）。
- **缺失维度按可用维度归一化，绝不记零分**（`w_used` 为实际参与权重，`missing` 列出缺失项，`confidence` 在可用维度 ≤3 时降为 low）。
- 货币 ETF 估值维度固定 60 分并标「不适用」。
- 五级信号：优先关注 80–100 / 买入观察 70–79 / 持有 50–69 / 谨慎持有 35–49 / 减仓观察 0–34（`signal_band` 返回 `{label, code, ...}`）。
- `cross_section(rows)` 追加 `rank` / `pct_rank`。

## 5. 四套策略（`strategies.py`，版本 `strategy-v5-2026-11-07`）

| 策略 | 规则要点 | 本次实跑结果 |
|---|---|---|
| A 质量筛选 | 成立年限/规模/20 日均额/价差/跟踪误差/费率/折溢价/结构 → 合格·观察·淘汰 + 淘汰原因 + 同类排名 | 合格 7 / 观察 3 / 淘汰 0 |
| B 趋势 | 收盘 > MA60 且 MA20 > MA60 且中期动量为正，需连续确认（默认 3 日） | 通过 3 只 |
| C 多周期动量 | 20/60/120 日动量横截面先转标准分再加权，月频换仓、次日收盘可成交 | 入选 516310 / 563020 / 511010 |
| D 稳健多资产 | 核心 50% + 防御 25% + 黄金 10% + 卫星 10% + 现金 5%，保守/稳健/平衡三模板，战略配置与战术择时分离 | 投资 57.01% + 现金 42.99%（`稳健` 模板，已叠加防守乘数 0.60） |

D 策略权重为**测试示例**，非个人投资建议；模板可通过 `v5.d_template` 切换。

## 6. 市场状态与压力测试

- `regime_engine`：趋势 0.40 / 广度 0.30 / 波动 0.30 → ≥65 进攻 ×1.00、≥40 中性 ×0.80、<40 防守 ×0.60。本次 **防守 30.3 分，乘数 0.60**（趋势 17.3 / 广度 40.0 / 波动 38.0，站上 MA60 仅 4/10）。
- `stress`：六情景（股市快速下跌、利率大幅上升、汇率剧烈波动、黄金与股票同跌、流动性枯竭、多资产相关性突升）+ 历史最差 20 日窗口 + 蒙特卡洛（normal / t / bootstrap，5000 次，seed 42）+ 相关性突升。本次极端最差「股市快速下跌 −14.16%」，历史最差窗口 2026-07-02 −3.79%。
- 结论判定**不只看正态假设**：尾部差异 0.60pp，本次样本下正态未明显低估尾部，但仍以 bootstrap 为准。

## 7. 四级风控（`risk_control.py`）

单只检查（波动上限 30%、回撤上限 −25%、20 日均额 ≥2000 万、折溢价上限 1%）→ 组合检查（年化波动、MDD、HHI 集中度、单只权重上限、平均相关性 ≤0.75）→ 风险等级与乘数（低/中/高）→ 四级熔断（green/yellow/orange/red，按回撤 −8/−15/−25 与波动 12/18/25 触发）。

本次：**低风险、波动 9.72%、MDD −7.85%、VaR95 −1.046%/日、CVaR95 −1.435%/日、HHI 1238、最大单只 20.00%、熔断 green**。

## 8. 决策中心（`decision_center.py`）

五道闸门（数据 / 规则 / 市场 / 风险 / 流动性），每条建议 12 字段：动作、方向、当前→目标权重、目标金额与数量、预计成本、交易成本占比、理由、风险提示、失效条件、`valid_until`、门禁结果、复盘状态。

**PRD 15.4 硬前置**：投资期限与最大可承受回撤未确认时，`risk_tolerance_configured=False`，目标权重与数量置 `None`，动作统一为「仅观察（未确认投资期限与最大回撤）」。本次 10 条建议全部落在此状态，`valid_until=2026-11-09`。

## 9. 回测（`backtest.py`，如实呈现、不美化）

防作弊七项 + 滚动样本外 + 参数敏感性，CLI：`python backtest.py output/price_history_515180.csv --deep`。

真实样本结论（515180，1666 交易日 ≈6.88 年，成本 0.002/边）：

| 指标 | 策略 | 买入持有 |
|---|---|---|
| 年化 | **−5.24%** | +4.64% |
| 最大回撤 | −36.01% | −22.07% |
| Sharpe / Sortino / Calmar | −0.76 / −0.57 / −0.15 | — |
| CVaR95 | −1.734%/日 | — |
| 年度胜率 | 12.5% | — |
| 持仓暴露 / 换手 | 34.9% / 4.1 次/年（累计成本 4.488%） | — |
| 期末净值 | 0.6904 | 1.3663 |

防前视校验：比对 1545 日、不一致 0 → 通过；样本内 −6.14%（MDD −34.15%），样本外 −3.11%（MDD −9.05%）；回撤持续 806 个交易日**未恢复**；滚动样本外 3 折 −6.31% / −2.13% / −0.37%，**样本外为正 0/3**；参数敏感性年化 −3.74%（区间 −5.24%~−1.23%，std 1.8pp）→ 判定**不稳定**。

**结论：MA20/60/120 多头排列在本样本上无效，不构成可交易优势。**该结论同时写入看板「回测」区块，不做美化，也不据此生成买卖建议。

## 10. API（13 个 REST 端点，双载体同一契约）

PRD 要求 FastAPI；本机无 `fastapi/uvicorn/pydantic/sqlalchemy/scipy/apscheduler`（本轮 `importlib.util.find_spec` 复查仍全为 False），因此采用**同一套响应契约 + 两种载体**：

- 载体 A（静态）：`api_layer.write_snapshot()` 写 `output/api/v1/*.json`，供 GitHub Pages 前端直接 XHR 读取（每日由 Actions 重新生成并提交）。
- 载体 B（实时）：`python api_server.py --port 8010`，标准库 `http.server` 提供同路径同结构响应；`--live` 时写操作真正落盘，否则 dry_run。

| # | 方法 | 路径 | 快照文件 |
|---|---|---|---|
| 1 | GET | `/api/v1/market/status` | `market_status.json` |
| 2 | GET | `/api/v1/etfs` | `etfs.json` |
| 3 | GET | `/api/v1/etfs/{code}` | `etf_detail.json` |
| 4 | GET | `/api/v1/etfs/{code}/signals` | `etf_signals.json` |
| 5 | GET | `/api/v1/rankings` | `rankings.json` |
| 6 | GET | `/api/v1/portfolio` | `portfolio.json` |
| 7 | GET | `/api/v1/portfolio/risk` | `portfolio_risk.json` |
| 8 | GET | `/api/v1/recommendations` | `recommendations.json` |
| 9 | GET | `/api/v1/backtests` | `backtests.json` |
| 10 | GET | `/api/v1/data/health` | `data_health.json` |
| 11 | POST | `/api/v1/portfolio/positions` | `_post_positions_schema.json` |
| 12 | POST | `/api/v1/recommendations/{id}/review` | `_post_review_schema.json` |
| 13 | POST | `/api/v1/backtests/run` | `_post_backtest_schema.json` |

统一信封：`data_date / updated_at / sources / model_versions / api_version / disclaimer / data`。自测：`python api_layer.py selftest`（8/8）、`python api_server.py --selftest`（12/12，起临时端口跑真实 HTTP 往返）。

## 11. 前端（11 页，零构建静态多页）

深色金融终端风格（`frontend/assets/theme.css`），原生 XHR 读取 `../output/api/v1/*.json`，无构建步骤。

图表组件 `frontend/assets/charts.js`（ES5，IIFE 暴露 `Charts.line/bar/hbar`）：优先动态加载 jsdelivr ECharts 5.5.0 CDN（6 秒超时/失败自动降级到自写 canvas 渲染，含网格、刻度、图例、多系列折线、分组柱、横向柱、设备像素比处理）；`yAxis.scale=true` 保证净值类窄幅序列使用自然刻度。页面脚本带版本查询（`?v=6`）以规避浏览器强缓存。

| 页面 | 文件 | 数据源 |
|---|---|---|
| 投资驾驶舱 | `dashboard.html` | market_status + portfolio_risk + portfolio + rankings + recommendations |
| 市场分析 | `market.html` | market_status |
| ETF 筛选 | `screener.html` | rankings（资产类别/信号/质量三档筛选） |
| ETF 详情 | `etf.html` | etf_detail（五维明细 + 建议 + 规则） |
| 策略中心 | `strategy.html` | portfolio.strategies（A/B/C/D） |
| 回测中心 | `backtest.html` | backtests（指标 + 防作弊 + 样本外） |
| 组合管理 | `portfolio.html` | portfolio（风险预算权重 + 整手计划 + 约束） |
| 风险管理 | `risk.html` | portfolio_risk（等级/熔断/历史窗口/六情景/蒙特卡洛） |
| 交易决策 | `decision.html` | recommendations（五闸门 + 理由 + 失效日） |
| 投资复盘 | `review.html` | recommendations + data_health（含 10 项验收自查） |
| 系统设置 | `settings.html` | data_health（库表计数 + 数据源分级） |

数据未生成或取数失败时，页面统一显示「数据异常，暂停决策」，不展示任何有效信号（PRD 15.5）。

## 12. 六阶段开发计划与完成状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| ① 基础设施 | 数据库、数据分级、规则层 | 完成（SQLite 20 表、P0/P1/P2 十源、`trading_rules`） |
| ② 量化计算 | 指标、五维评分、横截面 | 完成（含 `indicator_daily` 逐日回填 19293 行） |
| ③ 策略与风控 | 四策略、三档状态、六情景压力、四级熔断 | 完成（自测 10/10、6/6、8/8） |
| ④ 交易决策 | 五闸门、12 字段、失效条件、复盘 | 完成（10 条建议，全部按 15.4 仅观察） |
| ⑤ 回测与验证 | 防前视、滚动样本外、敏感性、契约测试 | 完成（防作弊通过；`tests/test_v5_modules.py` 15 项 OK） |
| ⑥ 系统优化 | API 双载体、11 页前端、CI 与 Pages | 完成（13 端点、11 页、`daily.yml` 已提交快照并发布前端） |

## 13. 验收（PRD 10 项，机器可复核）

| # | 验收项 | 结果 | 依据 |
|---|---|---|---|
| 1 | 数据可追溯 | pass | 每条数据带 `data_date/sources/model_versions`；`data_ingestion_log` 每源一行 |
| 2 | 筛选可解释淘汰原因 | pass | 策略 A 输出合格/观察/淘汰 + 未通过项 + 同类排名 |
| 3 | 指标与独立计算一致 | pass | `backtest.xcheck` 与独立 MA 实现偏差 0.0000%；15 项契约测试 |
| 4 | 策略有版本规则 | pass | `strategy-v5-2026-11-07` + `strategy_version` 表（留痕接口就绪） |
| 5 | 回测无未来函数与虚构成交 | pass | 比对 1545 日 0 不一致；仅用次日收盘成交、含成本与滑点 |
| 6 | 风控可测回撤与集中度 | pass | MDD −7.85%、HHI 1238、单只 20.00%、四级熔断 |
| 7 | 仓位考虑现金成本与最小单位 | **partial** | 整手 `Q=floor(B/(P×L))×L` 与全成本已实现；但 `capital_cny` 与风险偏好为空，按 15.4 不输出数量 |
| 8 | 决策有解释与失效条件 | pass | 12 字段 + 理由 + `valid_until=2026-11-09` |
| 9 | 记录可追溯 | pass | `recommendation` 10 条，`review_status` 默认 pending，人工确认后写回 |
| 10 | 数据异常不出有效信号 | pass | 数据质量闸门 + 规则闸门（缺失 0 只、未核验 10 只）；异常显示「数据异常，暂停决策」 |

第 7 项为 partial 属**设计行为**：只有补齐 `v5.risk_profile.investment_horizon_years`、`max_drawdown_tolerance_pct` 与 `v5.capital_cny` 后才解锁目标金额与数量。

## 14. 运行方式

```powershell
# 主流程（云端参数，约 20–30 秒）
python etf_monitor.py --cloud

# 离线分层自检（12 区块 + 13 端点 + 库表 + 验收）
python _v5_selftest.py

# 各层自测
python factor_engine.py selftest      # 其余模块同理
python api_server.py --selftest
python -m unittest tests.test_v5_modules -v

# 回填指标历史（幂等）
python backfill_indicators.py

# 本地前端（前端直接读静态快照）
python -m http.server 8011 --bind 127.0.0.1
# 打开 http://127.0.0.1:8011/frontend/dashboard.html

# 实时 API（写操作需加 --live）
python api_server.py --port 8010
```

线上入口（推送后生效）：
- 前端驾驶舱 `https://xhxie123-maker.github.io/etf-monitor-V3/frontend/dashboard.html`
- 单页看板 `https://xhxie123-maker.github.io/etf-monitor-V3/output/etf_dashboard.html`

## 15. 已知限制与技术载体偏离（如实登记）

| 项 | PRD 要求 | 实际实现 | 原因 |
|---|---|---|---|
| 数据库 | PostgreSQL | SQLite（20 表同关系模型） | 运行环境无常驻服务；GitHub Pages 纯静态 |
| API | FastAPI + Pydantic | 同一契约 + 静态快照 / 标准库 `http.server` | 本机未安装 fastapi/uvicorn/pydantic/sqlalchemy |
| 前端 | Vue3 + TS + Vite + ECharts + Element Plus | 零构建静态多页（11 页 + 原生 XHR；图表 ECharts CDN + canvas 回退） | GitHub Pages 无构建链；CDN 失败仍可自绘 |
| 调度 | APScheduler | GitHub Actions cron（工作日 15:35） | 无长驻进程 |
| ATR / ADX | `indicator_daily` 含 ATR/ADX | 字段留空 | 数据源仅 `date, close`，无 high/low，不用估算值填充 |
| 品种规则 | 逐只核验 T+0/T+1、盘后固定价格资格 | 10 只全部 `verified=False`，`rule_gate` 返回 warn | 无稳定可核验的官方规则接口；不臆造 |
| 资金规模与风险偏好 | 目标金额与数量 | 空值，仅输出排名/权重/风险指标 | 需用户本人确认（PRD 15.4） |
| 932000 指数 | 覆盖 | 本机与云端均取不到 | 已登记为已知数据缺口 |
| `smart_etf.db` | 随仓库持久化 | 未入 git（约 3.8MB） | 体积与必要性待评估；可由 Actions 每次重建 |
| 深度回测覆盖 | 每只标的滚动样本外 + 参数敏感性 | CI 仅对主标的 510300 跑 `--deep`，其余标的 `rolling_oos`/`sensitivity` 为 `null`，前端标注「未执行深度回测，不适用」 | 控制 Actions 时长（全标的 deep 约 20+ 分钟）；本地可逐只执行 |

## 16. 从 V4.1 到 V5.0 的变更清单

- 新增 13 个模块：`factor_engine` / `strategies` / `regime_engine` / `stress` / `risk_control` / `position_sizing` / `decision_center` / `trading_rules` / `smart_db` / `data_sources` / `api_layer` / `api_server` / `v5_layer`。
- `backtest.py` 扩写：`_recovery`、`no_lookahead_check`、`rolling_out_of_sample`、`sensitivity`、`--deep`。
- `etf_monitor.py` 四处接线：`price_map_full` 全量 DataFrame、插入 V5 构建块、模板插入 `{v5_html}`、标题升 `V5.0`。
- 新增 `backfill_indicators.py`、`tests/test_v5_modules.py`、`frontend/`（11 页 + 资产）。
- `.github/workflows/daily.yml`：`git add` 增加 `output/api`；Pages artifact 复制 `frontend/`，根路径重定向到 `frontend/dashboard.html`。
- `portfolio_config.json`：新增 `v5` 块（风险偏好、资金、单只/组合阈值、熔断阈值、成本惩罚、`d_template` 模板选择）。

### 本轮补齐（2026-11-07）

- `smart_db.py`：新增 7 个 sync（`strategy_definitions/versions`、`trading_calendar`、`profile_snapshot`、`portfolio`、`portfolio_daily`、`position`）；`counts()` 扩至全 20 表；`portfolio_daily` 加 `quality_flag` 并支持旧库自动迁移。
- `v5_layer.py`：新增 `_hypothetical_nav()` 假设组合净值回放（近 260 日，非实盘），DB 块写入 portfolio / A/B/C/D 定义与版本 / 交易日历 / 产品快照 / 假设净值；参数哈希改用 `hashlib.md5`；异常路径补 rollback+close。
- `api_layer.py`：portfolio 端点增加 `hypothetical_nav` 与 `hypothetical_nav_note`。
- 新增 `frontend/assets/charts.js`（ECharts CDN + canvas 回退）；`app.js` 六页接入图表（驾驶舱/市场/策略/回测/组合/风险）；`_gen_frontend.py` 模板同步加载 charts.js。
- 验收变化：#1、#9 由 partial 转 pass，当前 9 pass + 1 partial（#7 仍受 PRD 15.4 未确认风险偏好所限）。

> V4.1 的完整功能与口径说明见 git 历史（`3943efc`、`9fa3b2b`），本次重构未回退任何已上线能力。
