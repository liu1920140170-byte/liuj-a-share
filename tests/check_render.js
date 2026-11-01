/* 前端渲染测试：在 Node 里用 DOM/Canvas 桩真实执行 web/index.html 的脚本，
 * 喂入真实的 /api/analyze JSON，断言各个区块确实被渲染出来了。
 *
 *   node tests/check_render.js            # 需要先跑 python tests/gen_payload.py
 *   FAIL=1 node tests/check_render.js     # 模拟后端不可用
 *   EMPTY=1 node tests/check_render.js    # 模拟后端返回空数据
 *
 * 说明：浏览器在本机沙箱里无法启动（mojo IPC 被拦），所以这里用「执行脚本 + 桩化 DOM」
 * 的方式验证渲染路径：能跑通说明字段映射、图表绘制、交互绑定都是好的。
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const HTML_FILE = path.join(ROOT, "web", "index.html");
const PAYLOAD_FILE = path.join(__dirname, "_payload.json");

const html = fs.readFileSync(HTML_FILE, "utf8");
if (!fs.existsSync(PAYLOAD_FILE)) {
  console.error("缺少测试数据 " + PAYLOAD_FILE);
  console.error("请先运行： python tests/gen_payload.py 600519");
  process.exit(1);
}
const openTag = html.indexOf("<script>");
const closeTag = html.lastIndexOf("</script>");
if (openTag < 0 || closeTag < 0) {
  console.error("找不到 <script> 块");
  process.exit(1);
}
const js = html.slice(openTag + 8, closeTag);
const ids = [...html.slice(0, openTag).matchAll(/id="([^"]+)"/g)].map((m) => m[1]);

const calls = { fetch: [], texts: [], errors: [] };
const timers = [];
const TRACE = [];
global.TRACE = TRACE;

function makeCtx() {
  const noop = () => {};
  return {
    setTransform: noop, clearRect: noop, fillRect: noop, strokeRect: noop,
    beginPath: noop, moveTo: noop, lineTo: noop, quadraticCurveTo: noop, closePath: noop,
    stroke: noop, fill: noop, save: noop, restore: noop, setLineDash: noop,
    measureText: (t) => ({ width: String(t).length * 6 }),
    fillText(t) {
      if (typeof t !== "string") throw new Error("fillText 收到非字符串：" + t);
      if (/NaN|undefined/.test(t)) calls.errors.push("画布上出现异常文本：" + t);
      calls.texts.push(t);
    },
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "", textBaseline: "",
  };
}

function makeEl(id) {
  return {
    id, style: {}, className: "", textContent: "", _html: "", disabled: false,
    value: id === "codeInput" ? "600519" : "250",
    options: id === "barsSelect" ? [{ value: "120" }, { value: "250" }, { value: "500" }] : [],
    clientWidth: 1200, clientHeight: 560, offsetWidth: 200, offsetHeight: 90,
    _listeners: {},
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      if (typeof v !== "string") throw new Error("innerHTML 收到非字符串：#" + id);
      if (/undefined|NaN|\[object Object\]/.test(v)) {
        calls.errors.push("疑似脏数据渲染 #" + id + "：" + v.slice(0, 160));
      }
      this._html = v;
    },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; },
    getContext() { return makeCtx(); },
    focus() {}, select() {}, appendChild() {}, removeChild() {},
  };
}

const store = {};
ids.forEach((id) => { store[id] = makeEl(id); });

global.document = {
  readyState: "complete",
  body: { appendChild() {}, removeChild() {} },
  getElementById: (id) => (store[id] !== undefined ? store[id] : null),
  addEventListener() {},
  createElement: (id) => { const el = makeEl(id || "tmp"); store[el.id] = el; return el; },
  execCommand: () => true,
};
global.window = {
  devicePixelRatio: 2, addEventListener() {},
  location: { protocol: "http:", href: "http://127.0.0.1:8765/" },
};
global.location = global.window.location;
global.navigator = { clipboard: { writeText: () => Promise.resolve() } };
global.setTimeout = (fn) => { timers.push(fn); return timers.length; };
global.clearTimeout = () => {};

const payload = JSON.parse(fs.readFileSync(PAYLOAD_FILE, "utf8"));
// 让页面里的输入框与测试数据保持一致，这样断言里的请求 URL 才是对的
if (store.codeInput && payload.meta && payload.meta.code) store.codeInput.value = payload.meta.code;

global.fetch = (url) => {
  calls.fetch.push(String(url));
  if (process.env.FAIL === "1") return Promise.reject(new Error("connect ECONNREFUSED 127.0.0.1:8765"));
  if (process.env.HTTP500 === "1") {
    return Promise.resolve({ ok: false, status: 500, statusText: "Server Error",
      text: () => Promise.resolve(JSON.stringify({ ok: false, error: "后端崩溃" })) });
  }
  if (process.env.BADJSON === "1") return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve("<html>x</html>") });
  if (process.env.EMPTY === "1") {
    return Promise.resolve({ ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify({ ok: true, meta: {}, wyckoff: {}, decision: {}, kline: {} })) });
  }
  return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(payload)) });
};
process.on("uncaughtException", (e) => { calls.errors.push("uncaught: " + (e && e.stack ? e.stack : e)); });
// 重要：前端所有渲染都在 fetch().then() 异步链里，那里抛出的异常只会变成 unhandledRejection，
// 如果没有这个钩子，测试会「看起来正常但页面全是 --」。这曾经真实地掩盖过一个 bug。
process.on("unhandledRejection", (e) => {
  calls.errors.push("unhandledRejection: " + (e && e.stack ? e.stack : e));
});

function replaceAll(text, needle, repl) { return text.split(needle).join(repl); }

const jsPatched = js
  .replace("function renderMeta(d) {",
    "function renderMeta(d) { TRACE.push('renderMeta:d=' + (d ? Object.keys(d).length : 'null')"
    + " + ' metaKeys=' + (d && d.meta ? Object.keys(d.meta).length : 'nom')"
    + " + ' name=' + (d && d.meta ? String(d.meta.name) : 'nom')"
    + " + ' dates=' + (d && d.kline && d.kline.dates ? d.kline.dates.length : 'nod'));")
  .replace("  function setStatus(kind, text) {",
    "  function setStatus(kind, text) { TRACE.push('status:' + kind);")
  .replace("  function backendDown(err) {",
    "  function backendDown(err) { TRACE.push('backendDown:' + String(err).slice(0, 40));")
  .replace("  function renderReport(d) {",
    "  function renderReport(d) { TRACE.push('renderReport:' + str(obj(d).report_text).length);")
  .replace("  function load() {",
    "  function load() { TRACE.push('load');")
  .replace("  function renderAll() {",
    "  function renderAll() { TRACE.push('renderAll:' + (state.data ? Object.keys(state.data).join('|') : 'NULL'));")
  .replace("  function showError(msg) {",
    "  function showError(msg) { if (msg) TRACE.push('showError:' + String(msg).slice(0, 50));");
const jsPatched2 = replaceAll(jsPatched, "try { data = JSON.parse(txt); }",
  "try { data = JSON.parse(txt); TRACE.push('rawLen:' + txt.length"
  + " + ' metaType=' + (data && typeof data.meta)"
  + " + ' metaKeys=' + (data && data.meta ? Object.keys(data.meta).length : -1)"
  + " + ' name=' + (data && data.meta ? String(data.meta.name) : 'nom')"
  + " + ' klineDates=' + (data && data.kline && data.kline.dates ? data.kline.dates.length : -1)"
  + " + ' arrFn=' + (typeof arr) + ' objFn=' + (typeof obj)); }");
if (jsPatched2.indexOf("TRACE.push") < 0) calls.errors.push("TRACE 注入失败（前端函数名变了？）");

try { new Function(jsPatched2)(); } catch (e) { calls.errors.push("脚本执行抛错: " + e.stack); }

function strip(htmlText) { return String(htmlText || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim(); }

setImmediate(async () => {
  for (let i = 0; i < 60; i++) await Promise.resolve();
  timers.splice(0).forEach((fn) => { try { fn(); } catch (e) { calls.errors.push("timer: " + e.stack); } });
  for (let i = 0; i < 20; i++) await Promise.resolve();

  const checks = [];
  const check = (name, ok, detail) => checks.push({ name, ok: !!ok, detail: detail === undefined ? "" : String(detail) });

  const mode = process.env.FAIL === "1" ? "FAIL" : process.env.HTTP500 === "1" ? "HTTP500"
    : process.env.BADJSON === "1" ? "BADJSON" : process.env.EMPTY === "1" ? "EMPTY" : "NORMAL";

  check("脚本无异常", calls.errors.length === 0, calls.errors.slice(0, 3).join(" | "));
  check("发起了 /api/analyze 请求", calls.fetch.some((u) => u.indexOf("/api/analyze") >= 0), calls.fetch[0] || "无请求");

  if (mode === "NORMAL") {
    const meta = payload.meta, wk = payload.wyckoff, dec = payload.decision;
    check("行情摘要渲染了名称", String(store.metaBar._html).indexOf(meta.name) >= 0);
    check("行情摘要渲染了最新价", String(store.metaBar._html).indexOf(String(meta.price)) >= 0, meta.price);
    check("阶段文本正确", store.wyPhase.textContent === wk.phase_cn, store.wyPhase.textContent);
    check("子阶段渲染", String(store.wySub.textContent).indexOf(wk.sub_phase || "--") >= 0, store.wySub.textContent);
    check("置信度渲染", store.wyConfText.textContent === wk.confidence + "%", store.wyConfText.textContent);
    check("多空评分条有宽度", /%$/.test(store.scBull.style.width) && /%$/.test(store.scBear.style.width),
      store.scBull.style.width + " / " + store.scBear.style.width);
    check("趋势三档渲染", store.trDaily.innerHTML.length > 0 && store.trWeekly.innerHTML.length > 0
      && store.trIntraday.innerHTML.length > 0);
    check("威科夫补充信息含区间/量能", strip(store.wyNote.innerHTML).length > 20);
    check("决策动作正确", store.actLabel.textContent === dec.action_cn, store.actLabel.textContent);
    check("入场区间渲染", store.decEntry.textContent.indexOf("~") >= 0 || store.decEntry.textContent.length > 3,
      store.decEntry.textContent);
    check("止损渲染含依据", strip(store.decStop.innerHTML).length > 4, strip(store.decStop.innerHTML).slice(0, 60));
    check("风险收益比渲染", /[0-9]/.test(strip(store.decRR.innerHTML)), strip(store.decRR.innerHTML));
    check("仓位建议渲染", strip(store.decPos.innerHTML).length > 4, strip(store.decPos.innerHTML).slice(0, 50));
    check("目标位表格含 T1", store.decTargets._html.indexOf("T1") >= 0);
    check("检查清单条数一致",
      (store.decChecklist._html.match(/checkItem/g) || []).length === dec.checklist.length,
      (store.decChecklist._html.match(/checkItem/g) || []).length + " vs " + dec.checklist.length);
    check("理由/风险/失效条件非空",
      [store.decReasons, store.decWarnings, store.decInvalidations].every((el) => strip(el._html).length > 0));
    check("事件时间线含首个事件类型",
      strip(store.timeline._html).length > 10
      && (wk.events.length === 0 || strip(store.timeline._html).indexOf(wk.events[wk.events.length - 1].type_cn) >= 0));
    check("完整报告文本足够长", store.reportText.textContent.length > 500, store.reportText.textContent.length);
    check("免责声明渲染", store.disclaimer.textContent.length > 10);
    check("图表已绘制文字（日期/价格/事件）", calls.texts.length > 10, calls.texts.length + " 个文本");
    check("图表尺寸已设置", store.chart.width > 0 && store.chart.height > 0, store.chart.width + "x" + store.chart.height);
    check("画布上出现区间上下沿标注",
      calls.texts.some((t) => /区间/.test(t)) || calls.texts.some((t) => /Ice|Creek/.test(t)),
      calls.texts.filter((t) => /区间/.test(t)).slice(0, 2).join(","));
    check("图例渲染了均线", /MA/.test(strip(store.legend._html)), strip(store.legend._html).slice(0, 80));
    check("错误条未显示", store.errorBar.className.indexOf("show") < 0, store.errorBar.className);

    // 交互：鼠标悬停十字光标
    try {
      (store.chart._listeners["mousemove"] || []).forEach((fn) => fn({ clientX: 600, clientY: 200 }));
      check("悬停提示框显示", store.tip.style.display === "block", store.tip.style.display);
      check("悬停提示含日期与价格", /[0-9]{4}-[0-9]{2}-[0-9]{2}/.test(strip(store.tip._html)),
        strip(store.tip._html).slice(0, 70));
    } catch (e) { check("悬停交互", false, e.message); }

    // 交互：点击刷新
    try {
      const before = calls.fetch.length;
      (store.refreshBtn._listeners["click"] || []).forEach((fn) => fn({}));
      for (let i = 0; i < 30; i++) await Promise.resolve();
      check("刷新按钮触发新请求", calls.fetch.length > before, calls.fetch.length + " 次请求");
    } catch (e) { check("刷新交互", false, e.message); }
  } else {
    check("异常场景显示了错误提示", store.errorBar.className.indexOf("show") >= 0
      || strip(store.errorBar._html).length > 0 || strip(store.statusText.textContent).length > 0,
      strip(store.errorBar._html).slice(0, 90) || store.statusText.textContent);
  }

  const failed = checks.filter((c) => !c.ok);
  if (process.env.DEBUG === "1") {
    console.log("\n[debug] TRACE =", JSON.stringify(TRACE));
    console.log("[debug] statusText =", store.statusText.textContent);
    console.log("[debug] errorBar =", store.errorBar.className, "|", String(store.errorBar._html).slice(0, 200));
    console.log("[debug] metaBar =", String(store.metaBar._html).slice(0, 300));
    console.log("[debug] chartEmpty =", store.chartEmpty.style.display, store.chartEmpty.textContent);
    console.log("[debug] chartSub =", store.chartSub.textContent);
    console.log("[debug] wyPhase =", store.wyPhase.textContent);
    console.log("[debug] actLabel =", store.actLabel.textContent);
    console.log("[debug] htmlLens =", ["metaBar", "wyNote", "decTargets", "decChecklist", "timeline", "reportText"]
      .map((k) => k + ":" + String(store[k]._html !== undefined ? store[k]._html : store[k].textContent).length).join(" "));
  }
  console.log(`\n场景：${mode}   断言：${checks.length - failed.length}/${checks.length} 通过`);
  checks.forEach((c) => {
    console.log(`  ${c.ok ? "√" : "×"} ${c.name}${c.detail ? "  → " + c.detail : ""}`);
  });
  if (calls.errors.length) {
    console.log("\n运行期错误明细：");
    calls.errors.slice(0, 5).forEach((e) => console.log("  ! " + String(e).slice(0, 300)));
  }
  process.exit(failed.length ? 1 : 0);
});
