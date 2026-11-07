/* V5 前端共享运行时（零依赖 ES5 风格，可直接在 GitHub Pages 运行）
   数据来源：../output/api/v1/*.json（api_layer 每日生成的 13 端点静态快照）*/
(function () {
  var API = "../output/api/v1/";
  var PAGES = [
    ["dashboard", "投资驾驶舱", "dashboard.html"],
    ["market", "市场分析", "market.html"],
    ["screener", "ETF 筛选", "screener.html"],
    ["etf", "ETF 详情", "etf.html"],
    ["strategy", "策略中心", "strategy.html"],
    ["backtest", "回测中心", "backtest.html"],
    ["portfolio", "组合管理", "portfolio.html"],
    ["risk", "风险管理", "risk.html"],
    ["decision", "交易决策", "decision.html"],
    ["review", "投资复盘", "review.html"],
    ["settings", "系统设置", "settings.html"]
  ];

  function num(v, d) {
    d = d === undefined ? 2 : d;
    if (v === null || v === undefined || v === "" || isNaN(Number(v))) return "—";
    return Number(v).toFixed(d);
  }
  function sgn(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    var n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(2);
  }
  function color(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "d";
    var n = Number(v);
    return n > 0 ? "g" : (n < 0 ? "r" : "d");
  }
  function esc(s) {    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function bar(v, max, cls) {
    var pct = max ? Math.max(0, Math.min(100, (Number(v) || 0) / max * 100)) : 0;
    var c = cls || "b";
    var col = c === "g" ? "#22c55e" : c === "r" ? "#ef4444" : c === "a" ? "#f59e0b" : "#38bdf8";
    return '<div class="bar"><i style="width:' + pct.toFixed(1) + '%;background:' + col + '"></i></div>';
  }
  function tbl(head, rows) {
    return '<table class="t"><thead><tr>' + head.map(function (h) {
      return "<th>" + esc(h) + "</th>";
    }).join("") + "</tr></thead><tbody>" + rows.join("") + "</tbody></table>";
  }

  var CLS = { core_equity: "核心权益", defensive: "防御", diversifier: "分散器",
    style_satellite: "风格卫星" };
  function clsName(v) { return CLS[v] ? CLS[v] + "（" + v + "）" : (v || "—"); }
  function recTxt(v) {    if (v === null || v === undefined) return "—";
    if (typeof v === "object") {
      var dd = (v.days === undefined) ? (v.recovery_days === undefined ?
        (v.mdd_days === undefined ? "—" : v.mdd_days) : v.recovery_days) : v.days;
      var rd = (v.recover_days === undefined ? null : v.recover_days);
      return dd + " 交易日见底 / " + (v.recovered ? "已恢复" : "未恢复（距最高点 " + (rd === null ? "—" : rd) + " 日）");
    }
    return v + " 交易日";
  }

  function listTxt(v, sep) {
    sep = sep || "、";
    if (v === null || v === undefined || v === "") return "无";
    if (Object.prototype.toString.call(v) === "[object Array]") return v.join(sep) || "无";
    if (typeof v === "object") {
      var ks = Object.keys(v);
      if (!ks.length) return "无";
      return ks.map(function (k) { return k + "（" + String(v[k]) + "）"; }).join(sep);
    }
    return String(v);
  }

  function getJSON(name, cb, err) {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", API + name + ".json?_=" + Date.now(), true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { cb(JSON.parse(xhr.responseText)); } catch (e) { err("解析失败：" + e.message); }
      } else {
        err("HTTP " + xhr.status + " · " + name + ".json 未取到（快照尚未生成）");
      }
    };
    xhr.send();
  }

  function head(pageKey, title) {
    var nav = PAGES.map(function (p) {
      return '<a href="' + p[2] + '"' + (p[0] === pageKey ? ' class="on"' : "") + ">" + p[1] + "</a>";
    }).join("");
    return '<header class="top"><div class="topin"><div class="brand">Smart ETF Quant V5.0' +
      '<small>量化辅助决策系统 · 不构成投资建议</small></div><nav class="pages">' + nav +
      "</nav></div></header>";
  }

  function shell(pageKey, title) {
    var host = document.getElementById("app");
    host.innerHTML = head(pageKey, title) +
      '<div class="wrap"><main><h1 style="font-size:16px;margin:4px 0 12px">' + esc(title) +
      '</h1><div id="view">加载中…</div><footer class="ft">数据由 GitHub Actions 每日生成 · ' +
      "仅供参考，不构成投资建议</footer></main></div>";
    return document.getElementById("view");
  }

  function fail(view, msg) {
    view.innerHTML = '<div class="err"><b>数据异常，暂停决策</b><br>' + esc(msg) +
      '<div class="mini">按 PRD 15.5：行情未更新 / 规则缺失 / 估值过期 / 指标计算失败时必须停止输出有效信号。' +
      "请先确认 <code>python etf_monitor.py --cloud</code> 已生成 output/api/v1 快照。</div></div>";
  }

  function stamp(v, extra) {
    return '<div class="mini">数据日期 ' + esc(v.data_date || "—") + " · 更新时间 " +
      esc(v.updated_at || "—") + " · 数据源 " + esc((v.sources || []).join(" / ") || "—") +
      (extra || "") + "</div>";
  }

  /* ---------------------------------------------------------------- 各页渲染 */
  function dashboard(view) {
    getJSON("market_status", function (m) {
      getJSON("portfolio_risk", function (rk) {
        getJSON("rankings", function (r) {
          getJSON("portfolio", function (pf) {
            getJSON("recommendations", function (rc) {
              var md = m.data || {}, rd = rk.data || {}, pd = pf.data || {};
              var rl = rd.risk_level || {};
              var top = (r.data.items || []).slice(0, 5);
              var acts = (rc.data.items || []).filter(function (x) {
                return String(x.action || "").indexOf("仅观察") !== 0;
              });
              var kpis = [
                ["目标总敞口", num(pd.target_total_pct, 1) + "%",
                  "现金（含未投）" + num(100 - Number(pd.target_total_pct || 0), 1) + "%"],
                ["组合波动（年化）", num(rd.volatility_pct) + "%", "风险等级 " + (rl.label || "—")],
                ["最大回撤", num(rd.max_drawdown_pct) + "%", "VaR95 " + num(rd.var_95_daily_pct, 3) + "%/日"],
                ["市场状态", (md.label || "—") + " ×" + num(md.risk_multiplier, 2), "综合分 " + num(md.total_score, 1)],
                ["待执行建议", acts.length + " 条", pd.configured ? "风险偏好已确认" : "风险偏好未确认，仅观察"]
              ];
              var html = '<div class="card"><h2>投资驾驶舱<span class="code">PRD 第 11 章 顶部指标</span></h2>' +
                '<div class="kpis">' + kpis.map(function (k) {
                  return '<div class="kpi"><div class="k">' + esc(k[0]) + '</div><div class="v">' +
                    esc(k[1]) + '</div><div class="s">' + esc(k[2]) + "</div></div>";
                }).join("") + "</div>" + stamp(m) + "</div>";
              html += '<div class="grid2"><div class="card"><h2>评分前五</h2>' +
                tbl(["排名", "代码", "名称", "总分", "信号"], top.map(function (x) {
                  return "<tr><td>" + esc(x.rank) + "</td><td>" + esc(x.code) + "</td><td>" +
                    esc(x.name) + "</td><td>" + num(x.total_score, 1) + "</td><td>" +
                    esc(x.signal || "—") + "</td></tr>";
                })) + "</div>";
              html += '<div class="card"><h2>待执行决策<span class="code">已确认风险偏好后生效</span></h2>' +
                (acts.length ? tbl(["代码", "动作", "目标权重", "数量", "失效日"], acts.map(function (x) {
                  return "<tr><td>" + esc(x.etf_code) + "</td><td>" + esc(x.action) + "</td><td>" +
                    num(x.target_weight_pct, 2) + "</td><td>" +
                    esc(x.suggested_quantity === null ? "未生成" : x.suggested_quantity) +
                    "</td><td>" + esc(x.valid_until) + "</td></tr>";
                })) : '<div class="mini">当前无待执行建议：' + esc((rc.data.items || []).length) +
                  " 条决策全部为「仅观察（未确认投资期限与最大回撤）」，符合 PRD 15.4。</div>") + "</div></div>";
              html += '<div class="card"><h2>风险与熔断</h2><div class="mini">风险等级 ' + esc(rl.label || "—") +
                "（乘数 " + num(rl.multiplier, 2) + "）｜熔断 " + esc((rd.circuit_breaker || {}).label || "—") +
                "｜CVaR95 " + num(rd.cvar_95_daily_pct, 3) + "%/日｜HHI " + num((rd.concentration || {}).hhi, 0) +
                "｜最大单只权重 " + num((rd.concentration || {}).single_max_w_pct, 2) + "%</div>" +
                (rd.warnings && rd.warnings.length ? '<div class="warn">' + rd.warnings.map(esc).join("<br>") + "</div>" : "") +
                "</div>";
              view.innerHTML = html;
            }, function (e) { fail(view, e); });
          }, function (e) { fail(view, e); });
        }, function (e) { fail(view, e); });
      }, function (e) { fail(view, e); });
    }, function (e) { fail(view, e); });
  }

  function market(view) {
    getJSON("market_status", function (m) {
      var d = m.data || {};
      var parts = [["趋势", d.trend_score, 0.40], ["广度", d.breadth_score, 0.30], ["波动", d.volatility_score, 0.30]];
      var bd = d.breadth_detail || {};
      var html = '<div class="card"><h2>市场状态三档</h2><div class="kpis">' +
        [["状态", d.label || d.regime || "—", d.action || ""],
         ["综合分", num(d.total_score, 1), "进攻≥65 / 中性≥40 / 防守<40"],
         ["风险乘数", num(d.risk_multiplier, 2), "直接收缩风险预算总敞口"],
         ["站上 MA60", num(bd.above_ma60_pct, 1) + "%", "站上 MA20 " + num(bd.above_ma20_pct, 1) + "%"],
         ["指数 20 日波动", num(d.vol20_pct, 2) + "%", "历史分位 " + num(d.vol_pct_rank, 1)]
        ].map(function (k) {
          return '<div class="kpi"><div class="k">' + esc(k[0]) + '</div><div class="v">' + esc(k[1]) +
            '</div><div class="s">' + esc(k[2]) + "</div></div>";
        }).join("") + "</div>" + stamp(m) + "</div>";
      html += '<div class="grid2"><div class="card"><h2>三项分项得分</h2><table class="t"><tbody>' +
        parts.map(function (p) {
          return '<tr><td style="width:70px">' + p[0] + '</td><td style="width:46px">' + num(p[1], 1) +
            "</td><td>" + bar(p[1], 100, p[1] >= 60 ? "g" : (p[1] >= 40 ? "a" : "r")) +
            '</td><td class="d" style="width:52px">权重 ' + (p[2] * 100).toFixed(0) + "%</td></tr>";
        }).join("") + "</tbody></table>" +
        '<div class="mini">权重 = 趋势 0.40 / 广度 0.30 / 波动 0.30（regime_engine.WEIGHTS）</div></div>';
      html += '<div class="card"><h2>判定理由</h2><div class="mini">' +
        (d.reasons || []).map(function (x) { return "· " + esc(x); }).join("<br>") +
        "</div>" + (d.risk_state ? '<div class="mini">组合层面风险等级：' + esc(d.risk_state) +
          "｜熔断：" + esc(d.circuit_breaker) + "</div>" : "") + "</div></div>";
      view.innerHTML = html;
    }, function (e) { fail(view, e); });
  }

  function screener(view) {
    getJSON("etfs", function (r) {
      var items = (r.data || {}).items || [];
      var html = '<div class="card"><h2>ETF 筛选<span class="code">五维评分 + 质量分层</span></h2>' +
        '<div class="mini">可按资产类别 / 信号 / 质量筛选；分数按可用维度归一化，缺失维度不记零分。</div>' +
        '<div style="margin:8px 0"><select id="f_cls"><option value="">全部资产类别</option></select> ' +
        '<select id="f_sig"><option value="">全部信号</option></select> ' +
        '<select id="f_q"><option value="">全部质量</option></select> ' +
        '<button id="f_go">筛选</button> ' +
        '<span class="mini" id="f_n"></span></div><div id="tb"></div>' + stamp(r) + "</div>";
      view.innerHTML = html;
      var cls = {}, sig = {}, q = {};
      items.forEach(function (x) { cls[x.asset_class] = 1; sig[x.signal] = 1; q[x.quality] = 1; });
      Object.keys(cls).forEach(function (k) { addOpt("f_cls", k, k); });
      Object.keys(sig).forEach(function (k) { if (k !== "undefined") addOpt("f_sig", k, k); });
      Object.keys(q).forEach(function (k) { if (k !== "undefined") addOpt("f_q", k, k); });
      function addOpt(id, v, t) {
        var s = document.getElementById(id);
        var o = document.createElement("option"); o.value = v; o.textContent = t;
        s.appendChild(o);
      }
      function draw() {
        var c = document.getElementById("f_cls").value;
        var g = document.getElementById("f_sig").value;
        var qq = document.getElementById("f_q").value;
        var rows = items.filter(function (x) {
          return (!c || x.asset_class === c) && (!g || x.signal === g) && (!qq || x.quality === qq);
        });
        document.getElementById("f_n").textContent = "命中 " + rows.length + " / " + items.length + " 只";
        document.getElementById("tb").innerHTML = tbl(
          ["代码", "名称", "资产类别", "总分", "趋势", "动量", "估值", "流动性", "风险", "信号", "质量", "置信"],
          rows.map(function (x) {
            var s = x.sub_scores || {};
            return "<tr><td>" + esc(x.code) + "</td><td>" + esc(x.name) + "</td><td class='d'>" +
              esc(clsName(x.asset_class)) + "</td><td><b>" + num(x.total_score, 1) + "</b></td><td>" +
              num(s.trend, 1) + "</td><td>" + num(s.momentum, 1) + "</td><td>" + num(s.valuation, 1) +
              "</td><td>" + num(s.liquidity, 1) + "</td><td>" + num(s.risk, 1) + "</td><td>" +
              esc(x.signal || "—") + "</td><td>" + esc(x.quality || "—") + "</td><td class='d'>" +
              esc(x.confidence || "") + "</td></tr>";
          }));
      }
      document.getElementById("f_go").onclick = draw;
      draw();
    }, function (e) { fail(view, e); });
  }

  function etf(view) {
    getJSON("etf_detail", function (d) {
      getJSON("etf_signals", function (sg) {
        var items = (d.data || {}).items || {};
        var codes = Object.keys(items);
        var html = '<div class="card"><h2>ETF 详情</h2><div class="mini">选择标的查看五维分解、质量分层、策略权重与最新信号。</div>' +
          '<div style="margin:8px 0"><select id="c">' + codes.map(function (c) {
            return '<option value="' + esc(c) + '">' + esc(c) + " " + esc(items[c].name || "") + "</option>";
          }).join("") + "</select></div><div id=\"body\"></div>" + stamp(d) + "</div>";
        view.innerHTML = html;
        function draw() {
          var c = document.getElementById("c").value;
          var x = items[c] || {};
          var s = x.sub_scores || {};
          var sigs = ((sg.data || {}).items || {})[c] || [];
          document.getElementById("body").innerHTML =
            '<div class="kpis">' +
            [["总分", num(x.score, 1)], ["信号", x.signal || "—"], ["质量", (x.quality || "—") + " " + num(x.quality_score, 1)],
             ["置信度", x.confidence || "—"], ["策略权重", num(x.strategic_weight_pct, 2) + "%"],
             ["PE 分位", num(x.pe_pct, 2)]].map(function (k) {
              return '<div class="kpi"><div class="k">' + esc(k[0]) + '</div><div class="v">' + esc(k[1]) + "</div></div>";
            }).join("") + "</div>" +
            tbl(["维度", "得分", "权重"], [["趋势", s.trend, 0.25], ["动量", s.momentum, 0.20],
              ["估值", s.valuation, 0.15], ["流动性", s.liquidity, 0.15], ["风险", s.risk, 0.25]].map(function (p) {
                return "<tr><td>" + p[0] + "</td><td>" + num(p[1], 1) + "</td><td class='d'>" +
                  (p[1] === null || p[1] === undefined ? "缺失（不记零分）" : (p[2] * 100).toFixed(0) + "%") + "</td></tr>";
              })) +
            '<div class="mini">缺失维度：' + esc((x.missing_dims || []).join(" / ") || "无") +
            "｜资产类别：" + esc(x.asset_class || "—") + "｜角色：" + esc(x.role || "—") + "</div>" +
            (sigs.length ? tbl(["信号日", "失效日", "动作", "分数", "方向", "理由"], sigs.map(function (g) {
              return "<tr><td>" + esc(g.signal_date) + "</td><td>" + esc(g.valid_until) + "</td><td>" +
                esc(g.action) + "</td><td>" + num(g.score, 1) + "</td><td>" + esc(g.direction || "—") +
                "</td><td class='d'>" + esc((g.reasons || []).slice(0, 2).join("；")) + "</td></tr>";
            })) : '<div class="mini">暂无该标的的决策信号记录。</div>');
        }
        document.getElementById("c").onchange = draw;
        draw();
      }, function (e) { fail(view, e); });
    }, function (e) { fail(view, e); });
  }

  function strategy(view) {
    getJSON("portfolio", function (p) {
      var st = (p.data || {}).strategies || {};
      if (!st || !st.A) {
        view.innerHTML = '<div class="warn">策略中心数据未随快照输出（strategies 扩展字段为空）。</div>';
        return;
      }
      var A = st.A || {}, B = st.B || {}, C = st.C || {}, D = st.D || {};
      var html = '<div class="card"><h2>四策略中心<span class="code">A 质量筛选 / B 趋势 / C 多周期动量 / D 多资产稳健</span></h2>' +
        '<div class="mini">版本 ' + esc(st.version || "—") + "｜策略规则随版本发布，修改留痕（strategy_version 表）</div>" +
        '<div class="kpis">' + [["A 合格", (A.counts || {}).合格 || 0], ["A 观察", (A.counts || {}).观察 || 0],
          ["A 淘汰", (A.counts || {}).淘汰 || 0], ["B 通过", B.n_pass || 0],
          ["C 入选", (C.selected || []).length], ["D 投资比例", num(D.invested_pct, 2) + "%"]
        ].map(function (k) {
          return '<div class="kpi"><div class="k">' + esc(k[0]) + '</div><div class="v">' + esc(k[1]) + "</div></div>";
        }).join("") + "</div>" + stamp(p) + "</div>";
      html += '<div class="grid2"><div class="card"><h2>A · 质量筛选</h2>' +
        tbl(["代码", "判定", "同类排名", "未通过项"], ((A.items) || []).map(function (x) {
          return "<tr><td>" + esc(x.code) + "</td><td>" + esc(x.verdict) + "</td><td class='d'>" +
            esc(x.class_rank === null || x.class_rank === undefined ? "—" : x.class_rank + "/" + x.class_size) +
            "</td><td class='d'>" + esc((x.reasons || []).join("；") || "—") + "</td></tr>";
        })) + "</div>";
      html += '<div class="card"><h2>B · 趋势确认<span class="code">收盘>MA60 且 MA20>MA60 且中期动量为正，需连续确认</span></h2>' +
        tbl(["代码", "状态", "确认日", "60 日动量"], (B.items || []).map(function (x) {
          return "<tr><td>" + esc(x.code) + "</td><td>" + esc(x.state) + "</td><td>" + esc(x.days_confirmed) +
            "</td><td>" + sgn(x.mom60_pct) + "%</td></tr>";
        })) + "</div>";
      html += '<div class="card"><h2>C · 多周期动量<span class="code">月频换仓 / 下一交易日成交价 / 横截面转标准分</span></h2>' +
        tbl(["排名", "代码", "20 日", "60 日", "120 日", "综合分", "入选"], (C.items || []).map(function (x) {
          return "<tr><td>" + esc(x.rank) + "</td><td>" + esc(x.code) + "</td><td>" + sgn(x.mom20_pct) +
            "%</td><td>" + sgn(x.mom60_pct) + "%</td><td>" + sgn(x.mom120_pct) + "%</td><td>" +
            num(x.score, 2) + "</td><td>" + (x.selected ? '<span class="tag g">入选</span>' : '<span class="tag d">—</span>') +
            "</td></tr>";
        })) + '</div>';
      html += '<div class="card"><h2>D · 稳健多资产<span class="code">核心 50 / 防御 25 / 黄金 10 / 卫星 10 / 现金 5</span></h2>' +
        tbl(["标的", "目标权重"], Object.keys(D.weights || {}).map(function (k) {
          return "<tr><td>" + esc(k) + "</td><td>" + num(D.weights[k], 2) + "%</td></tr>";
        })) + '<div class="mini">现金 ' + num(D.cash_pct, 2) + "%｜投资 " + num(D.invested_pct, 2) +
        "%｜风险乘数 " + num(D.risk_multiplier, 2) + "｜模板 " + esc(D.template || "稳健") +
        "<br>示例权重仅为测试示例、非个人建议；战略配置与战术择时分离。</div></div></div>";
      view.innerHTML = html;
    }, function (e) { fail(view, e); });
  }

  function backtest(view) {
    getJSON("backtests", function (r) {
      var items = (r.data || {}).items || [];
      var html = '<div class="card"><h2>回测中心<span class="code">含防前视、滚动样本外与参数敏感性</span></h2>' +
        '<div class="mini">结论如实呈现，不做美化：若策略年化低于买入持有，即为该样本上的真实结论。</div>' +
        tbl(["代码", "样本数", "策略年化", "持有年化", "策略 MDD", "持有 MDD", "Sharpe", "Sortino", "Calmar", "CVaR95", "换手/年", "成本累计"],
          items.map(function (x) {
            if (x.error) return "<tr><td>" + esc(x.code) + "</td><td colspan='11' class='r'>回测失败：" + esc(x.error) + "</td></tr>";
            return "<tr><td>" + esc(x.code) + "</td><td>" + esc(x.n_days) + "</td><td class='" + color(x.strategy_ann_pct) +
              "'>" + sgn(x.strategy_ann_pct) + "%</td><td class='" + color(x.buyhold_ann_pct) + "'>" + sgn(x.buyhold_ann_pct) +
              "%</td><td class='r'>" + num(x.strategy_mdd_pct) + "%</td><td class='r'>" + num(x.buyhold_mdd_pct) +
              "%</td><td>" + num(x.sharpe, 3) + "</td><td>" + num(x.sortino, 3) + "</td><td>" + num(x.calmar, 3) +
              "</td><td>" + num(x.cvar95_daily_pct, 3) + "%</td><td>" + num(x.turnover_per_year, 1) + "</td><td>" +
              num(x.cost_total_pct, 3) + "%</td></tr>";
          })) + stamp(r) + "</div>";
      items.forEach(function (x) {
        if (x.error) return;
        var la = x.lookahead || {}, ro = x.rolling_oos || null, se = x.sensitivity || null;
        var rec = x.recovery;
        var recLine = recTxt(rec);
        html += '<div class="grid2"><div class="card"><h2>' + esc(x.code) + ' · 防作弊校验</h2><div class="mini">' +
          "防前视：比对 " + esc(la.checked === undefined ? "—" : la.checked) + " 日，不一致 " +
          esc(la.mismatches === undefined ? "—" : la.mismatches) + " 处（须为 0）｜结果 " +
          (la.ok ? '<span class="g">通过</span>' : '<span class="r">未通过</span>') +
          "<br>回撤恢复：" + esc(recLine) + "</div></div>";
        if (!ro || !se) {
          html += '<div class="card"><h2>' + esc(x.code) + ' · 滚动样本外</h2><div class="mini">' +
            "该标的本轮未执行深度回测（CI 默认只对主标的 510300 跑 --deep），" +
            "滚动样本外与参数敏感性不适用；本地可执行 <code>python backtest.py output/price_history_" +
            esc(x.code) + ".csv --deep</code> 复核。</div></div></div>";
          return;
        }
        var folds = (ro.folds || []).map(function (f) {
          var v = (f.test_cagr === undefined ? f.ann_pct : f.test_cagr);
          return sgn(v) + "%";
        }).join(" / ") || "—";
        var npos = (ro.positive_folds === undefined ? ro.n_positive : ro.positive_folds);
        var nfold = (ro.n_folds === undefined ? (ro.folds || []).length : ro.n_folds);
        var stable = se.stable === undefined ? (se.verdict === "稳健") : se.stable;
        html += '<div class="card"><h2>' + esc(x.code) + ' · 滚动样本外</h2><div class="mini">' +
          esc(nfold) + " 折样本外收益：" + folds +
          "｜样本外为正 " + esc(npos === undefined ? "—" : npos) + "/" + esc(nfold) +
          "<br>参数敏感性：" + (stable ? '<span class="g">稳健</span>' : '<span class="r">不稳定</span>') +
          "（年化区间 " + num(se.ann_min, 2) + "~" + num(se.ann_max, 2) + "%，标准差 " + num(se.ann_std, 2) + "pp）" +
          "<br>" + esc(ro.note || "") + "</div></div></div>";
      });
      view.innerHTML = html;
    }, function (e) { fail(view, e); });
  }

  function portfolio(view) {
    getJSON("portfolio", function (p) {
      var d = p.data || {};
      var w = d.weights_pct || {};
      var html = '<div class="card"><h2>组合管理<span class="code">风险预算权重（已乘市场状态乘数）</span></h2>' +
        '<div class="kpis">' + [["目标总敞口", num(d.target_total_pct, 2) + "%"],
          ["现金（含未投）", num(100 - Number(d.target_total_pct || 0), 2) + "%"],
          ["名义现金预留", num(d.cash_reserve_pct, 2) + "%"], ["市场乘数", num(d.state_multiplier, 2)],
          ["风险偏好已确认", d.configured ? '<span class="g">是</span>' : '<span class="lock">否</span>'],
          ["下单计划", d.order_plan && d.order_plan.length ? "已生成" : "未生成"]
        ].map(function (k) {
          return '<div class="kpi"><div class="k">' + esc(k[0]) + '</div><div class="v">' + k[1] + "</div></div>";
        }).join("") + "</div>" +
        (d.configured ? "" : '<div class="warn">未确认投资期限 / 最大可承受回撤 / 资金规模，按 PRD 15.4 只输出权重与排名，不输出目标金额与数量。缺口：' +
          esc(listTxt(d.unlock_need)) + "</div>") + stamp(p) + "</div>";
      html += '<div class="grid2"><div class="card"><h2>目标权重</h2>' +
        tbl(["代码", "权重", "", "风险预算", "σ"], Object.keys(w).sort(function (a, b) { return w[b] - w[a]; }).map(function (k) {
          return "<tr><td>" + esc(k) + "</td><td style='width:60px'>" + num(w[k], 2) + "%</td><td style='width:120px'>" +
            bar(w[k], 35, "b") + "</td><td>" + num((d.risk_budget_pct || {})[k], 2) + "</td><td class='d'>" +
            num((d.sigma_pct || {})[k], 1) + "%</td></tr>";
        })) + "</div>";
      html += '<div class="card"><h2>下单计划 / 约束</h2>' +
        (d.order_plan && d.order_plan.length ? tbl(["代码", "目标权重", "预算", "价格", "数量", "全成本", "可成交"],
          d.order_plan.map(function (o) {
            return "<tr><td>" + esc(o.code) + "</td><td>" + num(o.target_weight_pct, 2) + "%</td><td>" +
              num(o.budget_cny, 0) + "</td><td>" + num(o.price, 3) + "</td><td>" + esc(o.qty) + "</td><td>" +
              num(o.cash_used, 0) + "</td><td>" + (o.fill_ok ? '<span class="g">是</span>' : '<span class="a">否</span>') +
              "</td></tr>";
          })) : '<div class="mini">' + esc(d.order_plan_reason || "未生成下单计划") + "</div>") +
        '<div class="mini">整手公式 Q = floor(B/(P×L))×L；费用与滑点计入每手全成本；剔除标的：' +
        esc(listTxt(d.excluded)) + "</div></div></div>";
      view.innerHTML = html;
    }, function (e) { fail(view, e); });
  }

  function risk(view) {
    getJSON("portfolio_risk", function (r) {
      var d = r.data || {}, rl = d.risk_level || {}, br = d.circuit_breaker || {};
      var st = d.stress || {};
      var hist = ((st.historical || {}).worst) || [];
      var ext = ((st.extreme || {}).items) || [];
      var mc = st.monte_carlo || {};
      var cj = st.correlation_jump || {};
      var html = '<div class="card"><h2>风险管理<span class="code">四级风控 + 熔断 + 压力测试</span></h2>' +
        '<div class="kpis">' + [["风险等级", rl.label || "—", "乘数 " + num(rl.multiplier, 2)],
          ["年化波动", num(d.volatility_pct) + "%", "下行波动 " + num(d.downside_vol_pct) + "%"],
          ["最大回撤", num(d.max_drawdown_pct) + "%", "恢复 " + esc(recTxt(d.recovery_days))],
          ["VaR95 / CVaR95", num(d.var_95_daily_pct, 3) + "%", num(d.cvar_95_daily_pct, 3) + "%/日"],
          ["熔断等级", br.label || br.level || "—", "yellow/orange/red 三档"],
          ["最大相关性", num((d.correlation_risk || {}).max_corr, 3), "均值 " + num((d.correlation_risk || {}).avg_corr, 3)]
        ].map(function (k) {
          return '<div class="kpi"><div class="k">' + esc(k[0]) + '</div><div class="v">' + esc(k[1]) +
            '</div><div class="s">' + esc(k[2]) + "</div></div>";
        }).join("") + "</div>" +
        (d.warnings && d.warnings.length ? '<div class="warn">' + d.warnings.map(esc).join("<br>") + "</div>" : "") +
        stamp(r) + "</div>";
      html += '<div class="grid2"><div class="card"><h2>历史最差窗口（20 日）</h2>' +
        tbl(["结束日", "跌幅", "窗口"], hist.map(function (x) {
          return "<tr><td>" + esc(x.end_date) + "</td><td class='r'>" + num(x.loss_pct) + "%</td><td class='d'>" +
            esc(x.window_days) + " 日</td></tr>";
        })) + '<div class="mini">真实联合收益滑窗，非假设情景。</div></div>';
      html += '<div class="card"><h2>极端情景</h2>' + tbl(["情景", "组合损失"], ext.map(function (x) {
        return "<tr><td>" + esc(x.name) + "</td><td class='r'>" + num(x.loss_pct) + "%</td></tr>";
      })) + "</div>";
      html += '<div class="card"><h2>蒙特卡洛（含 t / 自助法）</h2><div class="mini">' +
        "normal：p05 " + num(((mc.generators || {}).normal || {}).p05_pct) + "%｜t：p05 " +
        num(((mc.generators || {}).t || {}).p05_pct) + "%｜bootstrap：p05 " +
        num(((mc.generators || {}).bootstrap || {}).p05_pct) + "%" +
        "<br>尾部差异 " + num(mc.tail_gap_pct) + "pp｜判定：" + esc(mc.verdict || "—") +
        "（" + esc(mc.n_sims || "—") + " 次 / " + esc(mc.horizon || 20) + " 日）</div></div>";
      html += '<div class="card"><h2>相关性突升</h2><div class="mini">当前组合波动 ' + num(cj.vol_now_pct) +
        "% → 相关性升至 0.9 时 " + num(cj.vol_jump_pct) + "%（膨胀 " + num(cj.vol_inflation_pct) + "%）</div></div></div>";
      html += '<div class="card"><h2>组合级检查项</h2><div class="mini">' +
        Object.keys(d.portfolio_checks || {}).map(function (k) {
          var v = d.portfolio_checks[k];
          return "· " + esc(k) + "：" + esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v);
        }).join("<br>") + "</div></div>";
      view.innerHTML = html;
    }, function (e) { fail(view, e); });
  }

  function decision(view) {
    getJSON("recommendations", function (r) {
      var items = (r.data || {}).items || [];
      var html = '<div class="card"><h2>交易决策<span class="code">五道闸门 + 理由 + 失效条件</span></h2>' +
        '<div class="mini">每条建议记录 recommendation_id，人工确认后回写 review_status，可追溯。</div>' +
        tbl(["ID", "代码", "名称", "动作", "当前→目标", "数量", "金额", "交易成本", "失效日", "状态"],
          items.map(function (x) {
            return "<tr><td class='d'>" + esc(x.recommendation_id) + "</td><td>" + esc(x.etf_code) + "</td><td>" +
              esc(x.etf_name) + "</td><td>" + esc(x.action) + "</td><td>" + num(x.current_weight_pct, 2) + " → " +
              num(x.target_weight_pct, 2) + "</td><td>" + esc(x.suggested_quantity === null || x.suggested_quantity === undefined ? "未生成" : x.suggested_quantity) +
              "</td><td>" + esc(x.estimated_cost === null || x.estimated_cost === undefined ? "—" : num(x.estimated_cost, 0)) +
              "</td><td class='d'>" + num(x.trade_cost, 2) + "</td><td>" + esc(x.valid_until) + "</td><td>" +
              '<span class="tag">' + esc(x.review_status) + "</span></td></tr>";
          })) + stamp(r) + "</div>";
      html += '<div class="card"><h2>决策理由与风险提示</h2>' + items.map(function (x) {
        return '<div style="border-bottom:1px solid #25324e;padding:8px 0"><b>' + esc(x.etf_code) + " " +
          esc(x.etf_name) + "</b> · <span class='d'>" + esc(x.direction || "") + "</span>" +
          '<div class="mini">理由：' + esc((x.reasons || []).join("；") || "—") + "</div>" +
          (x.risk_warning ? '<div class="mini a">风险：' + esc(x.risk_warning) + "</div>" : "") + "</div>";
      }).join("") + "</div>";
      view.innerHTML = html;
    }, function (e) { fail(view, e); });
  }

  function review(view) {
    getJSON("recommendations", function (r) {
      getJSON("data_health", function (h) {
        var items = (r.data || {}).items || [];
        var stat = {};
        items.forEach(function (x) { stat[x.review_status || "pending"] = (stat[x.review_status || "pending"] || 0) + 1; });
        var acc = ((h.data || {}).acceptance) || [];
        var html = '<div class="card"><h2>投资复盘<span class="code">记录可追溯 + 人工确认</span></h2>' +
          '<div class="kpis">' + Object.keys(stat).map(function (k) {
            return '<div class="kpi"><div class="k">' + esc(k) + '</div><div class="v">' + stat[k] + "</div></div>";
          }).join("") + "</div>" +
          '<div class="mini">复盘口径：建议发出价 → 到期/人工确认价；未确认的建议按 valid_until 自动过期（expire_past）。</div>' +
          tbl(["ID", "代码", "动作", "信号日", "失效日", "状态"], items.map(function (x) {
            return "<tr><td class='d'>" + esc(x.recommendation_id) + "</td><td>" + esc(x.etf_code) + "</td><td>" +
              esc(x.action) + "</td><td>" + esc(x.signal_date) + "</td><td>" + esc(x.valid_until) + "</td><td>" +
              esc(x.review_status) + "</td></tr>";
          })) + stamp(r) + "</div>";
        html += '<div class="card"><h2>PRD 验收自查（10 项）</h2>' +
          tbl(["#", "验收项", "状态", "依据"], acc.map(function (a) {
            var cls = a.status === "pass" ? "g" : (a.status === "partial" ? "a" : "r");
            return "<tr><td>" + esc(a.no) + "</td><td>" + esc(a.item) + "</td><td class='" + cls + "'>" +
              esc(a.status) + "</td><td class='d'>" + esc(a.evidence) + "</td></tr>";
          })) + "</div>";
        view.innerHTML = html;
      }, function (e) { fail(view, e); });
    }, function (e) { fail(view, e); });
  }

  function settings(view) {
    getJSON("data_health", function (h) {
      var d = h.data || {}, db = d.db || {}, src = d.sources || {};
      var counts = db.counts || {};
      var byTier = src.by_tier || {};
      var html = '<div class="card"><h2>系统设置<span class="code">数据层与库健康 / 数据源分级</span></h2>' +
        '<div class="kpis">' + [["库表行数（etf_daily_bar）", esc(counts.etf_daily_bar || 0)],
          ["最后行情日", esc((db.health || {}).last_bar_date || "—")],
          ["数据源整体", src.ok ? '<span class="g">正常</span>' : '<span class="a">降级</span>'],
          ["仅 P2 源风险", src.only_p2 ? '<span class="r">是</span>' : '<span class="g">否</span>'],
          ["失败采集条数", esc((db.health || {}).failed_ingestions || 0)]
        ].map(function (k) {
          return '<div class="kpi"><div class="k">' + esc(k[0]) + '</div><div class="v">' + k[1] + "</div></div>";
        }).join("") + "</div>" + stamp(h) + "</div>";
      html += '<div class="grid2"><div class="card"><h2>库表计数</h2>' +
        tbl(["表", "行数"], Object.keys(counts).map(function (k) {
          return "<tr><td>" + esc(k) + "</td><td>" + esc(counts[k]) + "</td></tr>";
        })) + "</div>";
      html += '<div class="card"><h2>数据源分级（P0 交易所/基金公司 · P1 指数机构 · P2 第三方）</h2>' +
        tbl(["层级", "说明", "使用源", "调用数"], Object.keys(byTier).map(function (k) {
          var t = byTier[k] || {};
          return "<tr><td>" + esc(k) + "</td><td class='d'>" + esc(t.label || "") + "</td><td>" +
            esc((t.sources || []).join("、") || "—") + "</td><td>" + esc(t.calls || 0) + "</td></tr>";
        })) +
        '<div class="mini">P2 不得作为唯一生产源；降级时标记 degraded_p2，全部源失联则不出有效信号。</div>' +
        (src.notes && src.notes.length ? '<div class="warn">' + src.notes.map(esc).join("<br>") + "</div>" : "") +
        "</div></div>";
      html += '<div class="card"><h2>本地服务与安全</h2><div class="mini">' +
        "实时 API：<code>python api_server.py --port 8010</code>（标准库，零依赖）｜线上：静态 JSON 快照 <code>output/api/v1/</code>" +
        "<br>凭证仅存后端环境变量；人工交易记录保留原始输入；模型计算失败不出默认信号；建议均标注为辅助信息。" +
        "</div></div>";
      view.innerHTML = html;
    }, function (e) { fail(view, e); });
  }

  var RENDER = { dashboard: dashboard, market: market, screener: screener, etf: etf, strategy: strategy,
    backtest: backtest, portfolio: portfolio, risk: risk, decision: decision, review: review, settings: settings };

  window.V5 = { PAGES: PAGES };
  document.addEventListener("DOMContentLoaded", function () {
    var page = document.body.getAttribute("data-page");
    var title = document.title.replace(" · Smart ETF Quant V5", "");
    var view = shell(page, title);
    if (RENDER[page]) RENDER[page](view); else view.innerHTML = '<div class="err">未知页面</div>';
  });
})();
