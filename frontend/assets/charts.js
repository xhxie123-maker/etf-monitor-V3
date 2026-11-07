/* Smart ETF Quant V5 图表组件（ES5，零构建）
   优先使用 jsdelivr ECharts 5.5.0 CDN；6 秒未加载或失败自动降级到自写 canvas 渲染。
   对外暴露：Charts.line(elId, cats, series, opts)
             Charts.bar(elId, cats, series, opts)
             Charts.hbar(elId, cats, series, opts)
             Charts.PALETTE
   所有方法幂等：重复调用会清空容器并重新渲染。*/
var Charts = (function () {
  "use strict";

  var PALETTE = ["#38bdf8", "#22c55e", "#f59e0b", "#ef4444", "#a78bfa", "#f472b6"];
  var CDN_URL = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js";
  var LOAD_TIMEOUT_MS = 6000;

  var TEXT_COLOR = "#94a3b8";
  var AXIS_COLOR = "#64748b";
  var GRID_COLOR = "#25324e";
  var BG_COLOR = "#111a2b";

  /* ------------------------------------------------ ECharts CDN 加载状态机 */
  var loadState = "idle"; /* idle | loading | ready | failed */
  var waiters = [];

  function finish(state) {
    loadState = state;
    var list = waiters.slice(0);
    waiters = [];
    for (var i = 0; i < list.length; i++) {
      try { list[i](state === "ready"); } catch (e) { /* ignore */ }
    }
  }

  function loadECharts(cb) {
    if (loadState === "ready") { cb(true); return; }
    if (loadState === "failed") { cb(false); return; }
    waiters.push(cb);
    if (loadState === "loading") return;
    loadState = "loading";

    var s = document.createElement("script");
    s.src = CDN_URL;
    s.async = true;
    var done = false;
    s.onload = function () {
      if (done) return;
      done = true;
      finish(typeof window.echarts === "object" ? "ready" : "failed");
    };
    s.onerror = function () {
      if (done) return;
      done = true;
      finish("failed");
    };
    var timer = window.setTimeout(function () {
      if (done) return;
      done = true;
      finish("failed");
    }, LOAD_TIMEOUT_MS);
    document.getElementsByTagName("head")[0].appendChild(s);
  }

  /* ------------------------------------------------ 通用工具 */
  function toNum(v) {
    var n = Number(v);
    return isNaN(n) ? null : n;
  }

  function niceTicks(min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    var span = max - min;
    var rawStep = span / Math.max(1, count || 5);
    var mag = Math.pow(10, Math.floor(Math.log(rawStep) / Math.LN10));
    var norm = rawStep / mag;
    var step;
    if (norm <= 1) step = 1 * mag;
    else if (norm <= 2) step = 2 * mag;
    else if (norm <= 5) step = 5 * mag;
    else step = 10 * mag;
    var start = Math.floor(min / step) * step;
    var out = [];
    var v = start;
    var guard = 0;
    while (v <= max + step * 0.0001 && guard < 50) {
      out.push(Number(v.toFixed(10)));
      v += step;
      guard++;
    }
    return out;
  }

  /* 数值域口径：
     量级跨度 >= maxV*0.5 时归零（计数/占比类），
     否则自然刻度并两端留 10% 余量（净值/价格类窄幅序列）。*/
  function valueRange(allVals, forceZero) {
    var minV = null, maxV = null, i;
    for (i = 0; i < allVals.length; i++) {
      var n = toNum(allVals[i]);
      if (n === null) continue;
      if (minV === null || n < minV) minV = n;
      if (maxV === null || n > maxV) maxV = n;
    }
    if (minV === null) { minV = 0; maxV = 1; }
    var span = maxV - minV;
    var zeroBaseline = forceZero || minV < 0 || span >= Math.abs(maxV) * 0.5;
    if (zeroBaseline) {
      if (minV > 0) minV = 0;
      if (maxV < 0) maxV = 0;
      if (minV === maxV) maxV = minV + 1;
      return { min: minV, max: maxV, zero: true };
    }
    var pad = span * 0.1 || Math.abs(maxV) * 0.01 || 1;
    return { min: minV - pad, max: maxV + pad, zero: false };
  }

  function collectValues(series) {
    var out = [];
    for (var i = 0; i < series.length; i++) {
      var d = series[i].data || [];
      for (var j = 0; j < d.length; j++) {
        if (d[j] !== null && d[j] !== undefined) out.push(d[j]);
      }
    }
    return out;
  }

  function setupCanvas(elId) {
    var el = document.getElementById(elId);
    if (!el) return null;
    el.innerHTML = "";
    el.style.position = "relative";
    var cv = document.createElement("canvas");
    cv.style.width = "100%";
    cv.style.height = "100%";
    cv.style.display = "block";
    el.appendChild(cv);
    var rect = el.getBoundingClientRect();
    var w = Math.max(50, Math.floor(rect.width));
    var h = Math.max(50, Math.floor(rect.height));
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.floor(w * dpr);
    cv.height = Math.floor(h * dpr);
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  /* ------------------------------------------------ canvas 回退：公共绘制 */
  function layout(box, hasLegend) {
    var padL = 52, padR = 14, padT = hasLegend ? 34 : 12, padB = 40;
    return {
      l: padL, r: box.w - padR, t: padT, b: box.h - padB,
      plotW: box.w - padL - padR, plotH: box.h - padT - padB
    };
  }

  function drawLegend(ctx, box, series) {
    ctx.font = "11px sans-serif";
    ctx.textBaseline = "middle";
    var x = 12;
    for (var i = 0; i < series.length; i++) {
      var name = series[i].name || ("系列" + (i + 1));
      ctx.fillStyle = PALETTE[i % PALETTE.length];
      ctx.fillRect(x, 17, 14, 4);
      ctx.fillStyle = TEXT_COLOR;
      ctx.fillText(name, x + 18, 17);
      x += 18 + ctx.measureText(name).width + 16;
    }
  }

  function drawGrid(ctx, lay, ticks, yAt, h) {
    ctx.font = "10px sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (var i = 0; i < ticks.length; i++) {
      var y = yAt(ticks[i]);
      ctx.strokeStyle = GRID_COLOR;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(lay.l, y);
      ctx.lineTo(lay.r, y);
      ctx.stroke();
      ctx.fillStyle = AXIS_COLOR;
      var label = Math.abs(ticks[i]) < 1e-9 ? "0" : String(Number(ticks[i].toFixed(3)));
      ctx.fillText(label, lay.l - 6, y);
    }
    ctx.strokeStyle = GRID_COLOR;
    ctx.beginPath();
    ctx.moveTo(lay.l, lay.t);
    ctx.lineTo(lay.l, lay.b);
    ctx.stroke();
  }

  function drawCats(ctx, lay, cats, h) {
    var n = cats.length;
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = AXIS_COLOR;
    var step = lay.plotW / Math.max(1, n);
    for (var i = 0; i < n; i++) {
      var x = lay.l + step * (i + 0.5);
      var label = String(cats[i]);
      if (label.length > 10) label = label.substring(0, 9) + "…";
      ctx.save();
      ctx.translate(x, lay.b + 7);
      if (n > 8) ctx.rotate(-0.5);
      ctx.fillText(label, 0, 0);
      ctx.restore();
    }
  }

  /* ------------------------------------------------ canvas 折线 */
  function canvasLine(elId, cats, series) {
    var box = setupCanvas(elId);
    if (!box) return;
    var ctx = box.ctx;
    ctx.fillStyle = BG_COLOR;
    ctx.fillRect(0, 0, box.w, box.h);
    var lay = layout(box, series.length > 1);
    var rng = valueRange(collectValues(series), false);
    var ticks = niceTicks(rng.min, rng.max, 5);
    function yAt(v) {
      return lay.b - (v - rng.min) / (rng.max - rng.min) * lay.plotH;
    }
    drawGrid(ctx, lay, ticks, yAt);
    drawCats(ctx, lay, cats);
    drawLegend(ctx, box, series);

    var n = cats.length;
    var step = lay.plotW / Math.max(1, n);
    for (var s = 0; s < series.length; s++) {
      var data = series[s].data || [];
      ctx.strokeStyle = PALETTE[s % PALETTE.length];
      ctx.lineWidth = 2;
      ctx.beginPath();
      var started = false;
      for (var i = 0; i < n; i++) {
        var val = toNum(data[i]);
        if (val === null) { started = false; continue; }
        var x = lay.l + step * (i + 0.5);
        var y = yAt(val);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }

  /* ------------------------------------------------ canvas 分组柱 */
  function canvasBar(elId, cats, series, horizontal) {
    var box = setupCanvas(elId);
    if (!box) return;
    var ctx = box.ctx;
    ctx.fillStyle = BG_COLOR;
    ctx.fillRect(0, 0, box.w, box.h);
    var lay = layout(box, series.length > 1);
    var rng = valueRange(collectValues(series), true);
    var ticks = niceTicks(rng.min, rng.max, 5);

    if (horizontal) {
      drawHBarBars(ctx, box, lay, cats, series, rng);
    } else {
      function yAt(v) {
        return lay.b - (v - rng.min) / (rng.max - rng.min) * lay.plotH;
      }
      drawGrid(ctx, lay, ticks, yAt);
      drawCats(ctx, lay, cats);
      drawLegend(ctx, box, series);
      var n = cats.length;
      var group = lay.plotW / Math.max(1, n);
      var ns = series.length;
      var barW = Math.max(2, group * 0.7 / ns);
      for (var i = 0; i < n; i++) {
        for (var s = 0; s < ns; s++) {
          var val = toNum((series[s].data || [])[i]);
          if (val === null) continue;
          var x = lay.l + group * i + group * 0.15 + barW * s;
          var y0 = yAt(Math.max(0, Math.min(val, rng.max)));
          var yBase = yAt(0);
          var top = Math.min(y0, yBase);
          ctx.fillStyle = PALETTE[s % PALETTE.length];
          ctx.fillRect(x, top, barW, Math.abs(yBase - y0));
        }
      }
    }
  }

  function drawHBarBars(ctx, box, lay, cats, series, rng) {
    /* 横向柱：仅支持单系列，类目在左 */
    var n = cats.length;
    var left = 110;
    var right = box.w - 14;
    var top = 12;
    var bottom = box.h - 12;
    var plotW = right - left;
    var plotH = bottom - top;
    function xAt(v) {
      return left + (v - rng.min) / (rng.max - rng.min) * plotW;
    }
    ctx.strokeStyle = GRID_COLOR;
    ctx.font = "10px sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    var ticks = niceTicks(rng.min, rng.max, 5);
    for (var t = 0; t < ticks.length; t++) {
      var x = xAt(ticks[t]);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.fillStyle = AXIS_COLOR;
      ctx.fillText(String(ticks[t]), x, bottom + 10);
    }
    var band = plotH / Math.max(1, n);
    var bh = Math.max(2, band * 0.65);
    ctx.textAlign = "right";
    for (var i = 0; i < n; i++) {
      var val = toNum((series[0].data || [])[i]);
      if (val === null) continue;
      var y = top + band * i + (band - bh) / 2;
      var x0 = xAt(0);
      var x1 = xAt(val);
      ctx.fillStyle = PALETTE[0];
      ctx.fillRect(Math.min(x0, x1), y, Math.abs(x1 - x0), bh);
      var label = String(cats[i]);
      if (label.length > 12) label = label.substring(0, 11) + "…";
      ctx.fillStyle = TEXT_COLOR;
      ctx.fillText(label, left - 8, y + bh / 2);
    }
  }

  /* ------------------------------------------------ ECharts 渲染 */
  function baseOption(cats, series, type, horizontal) {
    var isBar = type !== "line";
    var catAxis = {
      type: "category",
      data: cats,
      axisLabel: { color: AXIS_COLOR },
      axisLine: { lineStyle: { color: GRID_COLOR } }
    };
    var valAxis = {
      type: "value",
      scale: true,
      axisLabel: { color: AXIS_COLOR },
      splitLine: { lineStyle: { color: GRID_COLOR } }
    };
    var sList = [];
    for (var i = 0; i < series.length; i++) {
      sList.push({
        name: series[i].name || ("系列" + (i + 1)),
        type: type,
        data: (series[i].data || []).slice(0),
        itemStyle: { color: PALETTE[i % PALETTE.length] }
      });
    }
    var opt = {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        triggerOn: "click",
        renderMode: "richText",
        confine: true
      },
      grid: { left: 52, right: 16, top: series.length > 1 ? 38 : 16, bottom: 40, containLabel: true },
      series: sList
    };
    if (horizontal) {
      opt.xAxis = valAxis;
      opt.yAxis = catAxis;
      opt.grid = { left: 110, right: 16, top: 16, bottom: 36, containLabel: true };
      for (var k = 0; k < opt.series.length; k++) opt.series[k].type = "bar";
    } else if (isBar) {
      opt.xAxis = catAxis;
      opt.yAxis = valAxis;
    } else {
      opt.xAxis = catAxis;
      opt.yAxis = valAxis;
      for (var j = 0; j < opt.series.length; j++) {
        opt.series[j].smooth = false;
        opt.series[j].symbolSize = 6;
        opt.series[j].lineStyle = { width: 2 };
      }
    }
    if (series.length > 1) opt.legend = { textStyle: { color: TEXT_COLOR }, top: 4 };
    return opt;
  }

  function renderWithECharts(elId, cats, series, type, horizontal) {
    var el = document.getElementById(elId);
    if (!el) return;
    el.innerHTML = "";
    var inst = window.echarts.init(el);
    inst.setOption(baseOption(cats, series, type, horizontal));
    if (window.addEventListener) {
      window.addEventListener("resize", function () { inst.resize(); });
    }
  }

  /* ------------------------------------------------ 统一入口 */
  function normalizeSeries(series) {
    if (!series) return [];
    if (Object.prototype.toString.call(series) !== "[object Array]") series = [series];
    var out = [];
    for (var i = 0; i < series.length; i++) {
      var s = series[i];
      if (s && Object.prototype.toString.call(s.data) === "[object Array]") {
        out.push({ name: s.name, data: s.data });
      } else {
        out.push({ name: "系列" + (i + 1), data: s });
      }
    }
    return out;
  }

  function render(elId, cats, series, type, horizontal) {
    cats = cats || [];
    series = normalizeSeries(series);
    loadECharts(function (ok) {
      if (ok) {
        try {
          renderWithECharts(elId, cats, series, type, horizontal);
          return;
        } catch (e) { /* 落到 canvas */ }
      }
      if (type === "line") canvasLine(elId, cats, series);
      else canvasBar(elId, cats, series, horizontal);
    });
  }

  function line(elId, cats, series) {
    render(elId, cats, series, "line", false);
  }
  function bar(elId, cats, series) {
    render(elId, cats, series, "bar", false);
  }
  function hbar(elId, cats, series) {
    render(elId, cats, series, "bar", true);
  }

  return {
    PALETTE: PALETTE,
    line: line,
    bar: bar,
    hbar: hbar
  };
})();
