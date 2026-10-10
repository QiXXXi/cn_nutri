/* 把浏览器端引擎当命令行用，输出与 /api/search 同形的 JSON。
 *
 *     node tools/engine_cli.js <data 目录> '<查询>' '{"limit":20}'
 *     node tools/engine_cli.js <data 目录> --parse '<查询>'
 *     node tools/engine_cli.js <data 目录> --batch   # 从 stdin 读 [{...}]，写回 [...]
 *
 * tests/test_client_engine.py 靠它把 JS 引擎和 Python 参考实现逐条对拍——没有这个
 * 对拍，600 多行移植就只能靠肉眼保证「应该没写错」。
 *
 * --batch 不是图省事：词库 12.9 MB，解码 + 建偏移表要 2 秒上下，一条查询起一个
 * node 进程的话整套对拍要跑一分多钟。
 *
 * 这里**不碰网络**：直接读 tables.json 和 corpus.bin 调 CnNutri.fromBuffers()，
 * 与网页里 fetch 版走的是同一条解码路径（load() 也只是 fetch 完再调它）。
 */
"use strict";

const fs = require("fs");
const path = require("path");

require(path.join(__dirname, "..", "src", "cn_nutri", "web", "engine.js"));

function loadEngine(dataDir) {
  const tables = JSON.parse(fs.readFileSync(path.join(dataDir, "tables.json"), "utf8"));
  const raw = fs.readFileSync(path.join(dataDir, "corpus.bin"));
  // 拷成独立的 ArrayBuffer：Node 的 Buffer 是从内存池里切出来的，byteOffset
  // 往往不是 0，直接拿 .buffer 会让后面所有 TypedArray 的偏移全错。
  const ab = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
  return globalThis.CnNutri.fromBuffers(tables, ab);
}

function run(engine, job) {
  if (job.stats) return stats(engine);
  const q = job.q || "";
  return job.parse ? engine.parse(q) : engine.search(q, job.opts || {});
}

/** 只读表头与桶的统计量：跟查询结果无关，专门验解码器本身没数错。 */
function stats(engine) {
  const C = engine.C;
  const byLen = {};
  let total = 0;
  for (let L = 1; L <= C.maxLen; L++) {
    const n = C.buckets[L][1];
    if (n) byLen[L] = n;
    total += n;
  }
  return {
    version: globalThis.CnNutri.VERSION,
    n_chars: C.nChars, n_words: C.nWords, n_syls: C.nSyls,
    n_parts: C.nParts, max_len: C.maxLen,
    bucket_total: total, by_len: byLen,
    sort_modes: engine.sortModes, max_results: engine.maxResults,
    structures: engine.corpusInfo.structures,
    liushu_names: engine.corpusInfo.liushu_names,
    meta: engine.meta,
  };
}

function readStdin() {
  return fs.readFileSync(0, "utf8");
}

function main(argv) {
  const dataDir = argv[0];
  const rest = argv.slice(1);
  if (!dataDir || !rest.length) {
    process.stderr.write(
      "用法: node engine_cli.js <data 目录> '<查询>' ['{选项}']\n" +
      "      node engine_cli.js <data 目录> --parse '<查询>'\n" +
      "      node engine_cli.js <data 目录> --batch   # stdin 读 JSON 数组\n");
    return 2;
  }
  const engine = loadEngine(dataDir);

  if (rest[0] === "--batch") {
    const jobs = JSON.parse(readStdin());
    process.stdout.write(JSON.stringify(jobs.map(j => run(engine, j))));
    return 0;
  }
  if (rest[0] === "--parse") {
    process.stdout.write(JSON.stringify(engine.parse(rest[1] || ""), null, 1));
    return 0;
  }
  const opts = rest[1] ? JSON.parse(rest[1]) : {};
  process.stdout.write(JSON.stringify(engine.search(rest[0] || "", opts), null, 1));
  return 0;
}

process.exitCode = main(process.argv.slice(2));
