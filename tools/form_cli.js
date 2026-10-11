/* 把网页的**逐字表单**当状态机来驱动，好在 node 里回归那些「只有手点才触发」的 bug。
 *
 *     node tools/form_cli.js <web 目录>      # 从 stdin 读步骤数组，写回状态日志
 *
 * 步骤（JSON 数组，逐条执行，每条都产出一条状态记录）：
 *
 *     {"set": "*[氵]*"}                  在表达式框里敲进这串并派发 input（走真的 260ms 防抖）
 *     {"syncDsl": true}                  立刻同步一次表达式 -> 表单（等价按回车）
 *     {"click": "#clear-slots"}          触发该元素上由 init() 挂的 onclick
 *     {"type": {"i":0,"k":"literals","v":"张"}}   直接调页面的 applyInput（输入框的按键回调）
 *
 * 做法是**直接 eval 页面里那段内联脚本**，只给它垫一层最小 DOM——不是抄一份逻辑
 * 出来测。抄一份等于测抄件：页面改了、抄件没改，套子照样绿。代价是垫的 DOM 得跟
 * 页面用到的 API 对得上，页面一旦用新的 DOM API（比如 insertAdjacentHTML）这里会
 * 直接抛错——抛错好过静默测不到。
 *
 * 看不到的东西：表单里的 <input> 是 renderSlots 用 innerHTML 字符串生成的，垫的
 * DOM 里没有真元素，所以**断开/禁用**这类逐元素状态观察不到。能观察到的是
 * formLocked、#form-note 文案、表达式文本、提示区 HTML——renderSlots 的 disabled
 * 就是从 formLocked 一行推出来的，所以这个代理足够卡住状态机本身。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.resolve(process.argv[2] || path.join(__dirname, "..", "src", "cn_nutri", "web"));

// ---------------------------------------------------------------------------
// 最小 DOM
// ---------------------------------------------------------------------------

function makeEl(tag) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    value: "", textContent: "", innerHTML: "", className: "",
    checked: false, disabled: false, children: [],
    dataset: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) {
        if (on === undefined) on = !this._s.has(c);
        if (on) this._s.add(c); else this._s.delete(c);
        return on;
      },
    },
    _handlers: {},
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); },
    fire(t, ev) { for (const fn of this._handlers[t] || []) fn(ev || {}); },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { return c; },
    querySelector() { return makeEl("span"); },
    querySelectorAll() { return []; },
    focus() {}, blur() {}, click() {}, setAttribute() {}, removeAttribute() {},
    onclick: null, onchange: null, oninput: null,
  };
  return el;
}

/** 按选择器缓存：页面把 $ 定义成 document.querySelector，同一个 id 必须始终拿到
 *  同一个对象，否则 `$("#dsl").value = x` 之后就读不回来了。 */
function installDom() {
  const cache = new Map();
  const document = {
    querySelector(sel) {
      if (!cache.has(sel)) cache.set(sel, makeEl("div"));
      return cache.get(sel);
    },
    querySelectorAll() { return []; },
    createElement(tag) { return makeEl(tag); },
    _handlers: {},
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); },
  };
  globalThis.document = document;
  // 页面里那条部件表 fetch 链自带 .catch，这里恒拒绝即可（少了静态文件就走后端，
  // 后端也没有——反正这条与表单无关）。
  globalThis.fetch = () => Promise.reject(new Error("form_cli 不联网"));
  return document;
}

// ---------------------------------------------------------------------------
// 装载页面脚本
// ---------------------------------------------------------------------------

function loadEngine() {
  require(path.join(WEB, "engine.js"));
  const tables = JSON.parse(fs.readFileSync(path.join(WEB, "data", "tables.json"), "utf8"));
  const raw = fs.readFileSync(path.join(WEB, "data", "corpus.bin"));
  const ab = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
  return globalThis.CnNutri.fromBuffers(tables, ab);
}

function loadPage() {
  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const m = html.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/);
  if (!m) throw new Error("index.html 里找不到内联脚本");

  // 内联脚本是顶层 <script>，但顶层 let/const（slots、formLocked、ENG…）在 vm 里
  // 不落到 globalThis 上，外面读不到。所以**追加**一段探针，让它替我们看。
  // 只加不改：页面源码本身一个字没动。
  const probe = `
;globalThis.__page = {
  init(engine, structures, liushu){
    ENG = engine; STRUCTURES = structures; LIUSHU = liushu;
    boot = async () => {};          // 真 boot 要 fetch 词库；这里已经装好了
    init();
    syncFromDsl();                  // 真 boot 的尾巴
  },
  probe(){
    return { locked: formLocked, note: $("#form-note").textContent,
             dsl: $("#dsl").value, slots: slots.map(slotToDsl),
             notice: $("#parse-notice").innerHTML };
  },
  syncFromDsl, syncFromForm, applyInput, slotsOf: () => slots,
};`;
  vm.runInThisContext(m[1] + probe, { filename: path.join(WEB, "index.html") });
}

// ---------------------------------------------------------------------------
// 驱动
// ---------------------------------------------------------------------------

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function runStep(document, step) {
  if ("set" in step) {
    const t = document.querySelector("#dsl");
    t.value = step.set;
    t.fire("input");                       // 走页面自己挂的 260ms 防抖
    await sleep(400);
  } else if (step.syncDsl) {
    globalThis.syncFromDsl();
    await sleep(50);
  } else if ("click" in step) {
    const el = document.querySelector(step.click);
    if (!el.onclick) throw new Error(step.click + " 上没有 onclick");
    el.onclick();
    await sleep(50);
  } else if ("type" in step) {
    const { i, k, v } = step.type;
    const inp = makeEl("input");
    inp.dataset.i = String(i);
    inp.dataset.k = k;
    inp.value = v;
    globalThis.applyInput(inp);
    await sleep(50);
  } else {
    throw new Error("认不得的步骤：" + JSON.stringify(step));
  }
}

async function main() {
  const document = installDom();
  const engine = loadEngine();
  loadPage();

  const steps = JSON.parse(fs.readFileSync(0, "utf8"));
  globalThis.__page.init(engine, engine.corpusInfo.structures, engine.corpusInfo.liushu_names);
  await sleep(50);

  const log = [];
  for (const step of steps) {
    await runStep(document, step);
    log.push(Object.assign({ step }, globalThis.__page.probe()));
  }
  process.stdout.write(JSON.stringify(log));
}

main().catch(e => {
  process.stderr.write((e && e.stack) || String(e));
  process.exitCode = 1;
});
