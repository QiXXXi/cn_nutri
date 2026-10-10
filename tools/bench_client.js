/* 量一量浏览器端引擎跑一遍查询要多久（node 24 / V8）。
 *
 *     node tools/bench_client.js [data 目录] [每查询次数]
 *
 * 量的和 README「性能」那张表里 Python 一侧的查询是同一批，好直接对照。
 * 数值取**中位数**而不是平均：单次抖动大（GC、系统负载），中位数才稳。
 *
 * 注意 node 只是代理：浏览器里还有 JIT 预热差异、以及从 ArrayBuffer 建视图的
 * 一次性开销，真机上会略慢。这是量级估算，不是基准测试。
 */
"use strict";

const fs = require("fs");
const path = require("path");

require(path.join(__dirname, "..", "src", "cn_nutri", "web", "engine.js"));

const QUERIES = [
  "[a,an,ang][~3,~4][7-][独体,左右]",
  "[左右][左右][左右][左右]",
  "[7-][7-][7-][7-]",
  "?[~1]",
  "*[氵]*",
  "*[左右][7-]*",
];

function median(xs) {
  const s = xs.slice().sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function main(argv) {
  const dataDir = argv[0] || path.join(__dirname, "..", "src", "cn_nutri", "web", "data");
  const reps = +(argv[1] || 7);

  const tables = JSON.parse(fs.readFileSync(path.join(dataDir, "tables.json"), "utf8"));
  const raw = fs.readFileSync(path.join(dataDir, "corpus.bin"));
  const ab = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);

  let t0 = process.hrtime.bigint();
  const engine = globalThis.CnNutri.fromBuffers(tables, ab);
  const loadMs = Number(process.hrtime.bigint() - t0) / 1e6;

  // 预热：第一遍要把 JIT 焐热，不能算进结果
  for (const q of QUERIES) engine.search(q, { limit: 20 });

  console.log(`node ${process.version}   解码 ${(ab.byteLength / 1e6).toFixed(2)} MB ` +
              `用时 ${loadMs.toFixed(0)} ms（只发生一次）`);
  console.log("");
  console.log("  中位耗时   命中数   查询");

  for (const q of QUERIES) {
    const times = [];
    let total = 0;
    for (let i = 0; i < reps; i++) {
      const t = process.hrtime.bigint();
      const r = engine.search(q, { limit: 20 });
      times.push(Number(process.hrtime.bigint() - t) / 1e6);
      total = r.total;
    }
    console.log(`  ${median(times).toFixed(1).padStart(7)} ms  ${String(total).padStart(7)}   ${q}`);
  }
  return 0;
}

process.exitCode = main(process.argv.slice(2));
