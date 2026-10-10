/* cn_nutri 客户端引擎 —— query.py / matcher.py / rank.py / pinyin.py 的移植。
 *
 * 网页版默认走这条路：DSL 解析、36.8 万词全量扫描、排序，全在浏览器里完成，
 * 服务器只发一次静态文件。服务器侧的收益是**结构性**的——原先每个请求都要占着
 * GIL 扫一遍，24 并发时中位延迟从 135 ms 涨到 2.5 s（README「客户端引擎」有实测）。
 *
 * 三条铁律
 * --------
 * 1. **词汇表不在这里**。韵母表、声母表、结构名、全半角映射……全部来自
 *    web/data/tables.json，由 tools/build_client_data.py 从 Python 模块现导。
 *    将来加一个结构名或改一条映射，不会出现「改了一边忘了另一边」。
 *    这里只有**逻辑**。
 * 2. **按码位工作，不按 UTF-16 码元**。词库里有一批增补平面的字（𠀁 U+20001…），
 *    在 JS 里 `s[i]` 会把它们劈成两个代理码元，词长、字位、排序全错。所以：
 *    查询串一律 `Array.from()` 拆成码位数组，词一律用「字符表下标数组」表示，
 *    只在最后显示时才 `String.fromCodePoint()` 拼回去。
 * 3. **排序键的末位是词本身，而词唯一**，所以排序是全序，与候选遍历顺序无关。
 *    这也是词库文件能按词长重排（而非按 DB id 序）的前提。
 *
 * 与 Python 的差异是**有意的**且只有两处，都在 tests/test_client_engine.py 里
 * 被逐条比对钉住：
 *   - 释义（defs）可以不打进词库（--no-definitions），此时结果页没有释义。
 *   - pinyin_display 和 sort_aux 是现算的，不存（省 7 MB）。build_client_data.py
 *     的 --check 会验算这两个能否逐条复原。
 */
"use strict";

(function (global) {
  const ENGINE_VERSION = 1;

  const NULL8 = 0xff;
  const NULL16 = 0xffff;

  //: 每个段的数据都补齐到 4 字节边界。段首地址必须 4 对齐，否则 Uint32Array /
  //: Uint16Array 的构造函数会直接抛 RangeError——段长度不总是 4 的倍数。
  function align4(n) { return (n + 3) & ~3; }

  /* ====================================================================
   * 一、拼音（pinyin.py 的移植）
   * ==================================================================== */

  function makePinyin(T) {
    const FINALS = T.finals;                     // 有序数组，下标即身份
    const FINAL_SET = new Set(FINALS);
    const INITIALS = T.initials;
    const INITIAL_SET = new Set(INITIALS);
    const ALIASES = T.final_aliases;
    const DISPLAY = T.display_final;
    const ZERO = T.zero_initial;
    const TONE_MARKS = T.tone_marks;             // 带调字母 -> [基元音, 调号]
    const TONE_TABLE = T.tone_mark_table;        // 基元音 -> "āáǎà"
    const Y_MAP = T.y_map, W_MAP = T.w_map, ZERO_FORM = T.zero_form;
    const NASALS = new Set(T.syllabic_nasals);
    const PREFIXES = T.parse_prefixes;           // 已按长度降序

    function normUmlaut(s) {
      return s.split("u:").join("v").split("ü").join("v");
    }

    function displayFinal(f) {
      return DISPLAY[f] !== undefined ? DISPLAY[f] : f;
    }

    function canonicalFinal(f) {
      let s = normUmlaut(String(f).trim().toLowerCase());
      if (ALIASES[s] !== undefined) s = ALIASES[s];
      return FINAL_SET.has(s) ? s : null;
    }

    function canonicalInitial(i) {
      const s = String(i).trim().toLowerCase();
      if (s === "" || s === ZERO || s === "∅" || s === "0") return "";
      return INITIAL_SET.has(s) ? s : null;
    }

    /** 剥掉调号，返回 [无调拼音, 调号]；无调号时调号是 5（轻声）。 */
    function stripTone(s) {
      let tone = 5, out = "";
      for (const ch of s) {
        const hit = TONE_MARKS[ch];
        if (hit === undefined) out += ch;
        else { tone = hit[1]; out += hit[0]; }
      }
      return [out, tone];
    }

    /** 解析一个拼音音节，返回 [声母, 韵母, 声调]；不支持数字调与调号两种写法。 */
    function parseSyllable(syllable) {
      let s = String(syllable).trim().toLowerCase();
      if (!s) throw new Error("空音节");

      let tone = 5;
      const last = s[s.length - 1];
      if (last >= "0" && last <= "9") {
        tone = +last;
        s = s.slice(0, -1);
        const prev = s[s.length - 1];
        if (s && prev >= "0" && prev <= "9") throw new Error("非法音节: " + syllable);
      } else {
        [s, tone] = stripTone(s);
      }
      s = normUmlaut(s);
      if (!s) throw new Error("非法音节: " + syllable);

      if (NASALS.has(s)) return ["", "ng", tone];
      // CC-CEDICT 把「一下儿」的「儿」记作 r5，归一到 er，使 [er] 两种都命中
      if (s === "r") return ["", "er", tone];

      let initial = "", rest = s;
      for (const cand of PREFIXES) {
        if (s.startsWith(cand)) { initial = cand; rest = s.slice(cand.length); break; }
      }

      if (initial === "y") {
        rest = Y_MAP[rest] !== undefined ? Y_MAP[rest] : rest;
        initial = "";
      } else if (initial === "w") {
        rest = W_MAP[rest] !== undefined ? W_MAP[rest] : rest;
        initial = "";
      } else if ((initial === "j" || initial === "q" || initial === "x") &&
                 rest.startsWith("u")) {
        rest = "v" + rest.slice(1);              // j/q/x 后的 u 实际是 ü
      }

      const final = ALIASES[rest] !== undefined ? ALIASES[rest] : rest;
      if (!FINAL_SET.has(final)) throw new Error("非法音节: " + syllable);
      return [initial, final, tone];
    }

    /** 重组出带调号的拼音（zhǎng / yàng / yú / wén）。轻声不加号。 */
    function formatPinyin(initial, final, tone) {
      let word;
      if (initial === "") {
        word = ZERO_FORM[final] !== undefined ? ZERO_FORM[final] : displayFinal(final);
      } else if (initial === "j" || initial === "q" || initial === "x") {
        // j/q/x 后只可能是 ü 音，正字法上不写两点：jü -> ju
        word = initial + displayFinal(final).split("ü").join("u");
      } else {
        word = initial + displayFinal(final);
      }
      if (tone === 5 || !word) return word;

      // 调号优先级：a > o > e > 末位元音
      let idx = -1;
      for (const target of ["a", "o", "e"]) {
        idx = word.indexOf(target);
        if (idx >= 0) break;
      }
      if (idx < 0) {
        for (let i = word.length - 1; i >= 0; i--) {
          if (TONE_TABLE[word[i]] !== undefined) { idx = i; break; }
        }
      }
      if (idx < 0) return word;
      const mark = TONE_TABLE[word[idx]];
      if (mark === undefined) return word;
      return word.slice(0, idx) + mark[tone - 1] + word.slice(idx + 1);
    }

    return { finals: FINALS, initials: INITIALS, canonicalFinal, canonicalInitial,
             displayFinal, parseSyllable, formatPinyin };
  }

  /* ====================================================================
   * 二、查询语言（query.py 的移植）
   * ==================================================================== */

  const STROKE_SUFFIX_LE = "-−—";

  /** 按码位排序（Python 的 sorted 就是码位序；JS 默认的 sort 是 UTF-16 码元序，
   *  遇到增补平面的字会不一致——部件里就有 𠀁）。 */
  function cpCompare(a, b) {
    const A = Array.from(a), B = Array.from(b);
    for (let i = 0; i < A.length && i < B.length; i++) {
      const d = A[i].codePointAt(0) - B[i].codePointAt(0);
      if (d) return d;
    }
    return A.length - B.length;
  }
  function cpSorted(arr) {
    return arr.slice().sort(cpCompare);
  }

  function makeQuery(T, P) {
    const PUNCT = T.punct_map;
    const TONE_TOKENS = T.tone_tokens;
    const STRUCTURES = T.structures;
    const LIUSHU = T.liushu;
    const IDS_OPS = new Set(T.ids_operators);
    const RANGES = T.han_ranges;
    const MAX_STROKES = T.max_strokes;

    function isHan(ch) {
      const cp = ch.codePointAt(0);
      for (const [lo, hi] of RANGES) if (cp >= lo && cp <= hi) return true;
      return false;
    }

    const IS_ALNUM = /^[\p{L}\p{N}]$/u;
    const IS_SPACE = /^\s$/;

    function normalizeSymbols(text) {
      let out = "";
      for (const ch of text) {
        if (PUNCT[ch] !== undefined) { out += PUNCT[ch]; continue; }
        const cp = ch.codePointAt(0);
        out += (cp >= 0xff01 && cp <= 0xff5e) ? String.fromCodePoint(cp - 0xfee0) : ch;
      }
      return out;
    }

    /* ---- 约束模型 ---- */

    function CharConstraint(fields) {
      this.finals = new Set(fields.finals);
      this.initials = new Set(fields.initials);
      this.tones = new Set(fields.tones);
      this.strokes = fields.strokes || [];       // [[op, n], …]，保持书写顺序
      this.structures = new Set(fields.structures);
      this.liushu = new Set(fields.liushu);
      this.parts = new Set(fields.parts);
      this.ids = new Set(fields.ids);
      this.literals = new Set(fields.literals);
    }

    CharConstraint.prototype.isEmpty = function () {
      return !(this.finals.size || this.initials.size || this.tones.size ||
               this.strokes.length || this.structures.size || this.liushu.size ||
               this.parts.size || this.ids.size || this.literals.size);
    };

    /** 展开成笔画数集合；无笔画条件时返回 null。 */
    CharConstraint.prototype.strokeSet = function () {
      if (!this.strokes.length) return null;
      const out = new Set();
      for (const [op, n] of this.strokes) {
        if (op === "eq") out.add(n);
        else if (op === "le") for (let k = 0; k <= n; k++) out.add(k);
        else for (let k = n; k <= MAX_STROKES; k++) out.add(k);
      }
      return out;
    };

    function strokeToDsl(op, n) {
      return op === "le" ? n + "-" : (op === "ge" ? n + "+" : String(n));
    }

    /** 给前端逐字表单用的结构形（DSL 是给人看的，这个是给表单用的）。 */
    CharConstraint.prototype.toDict = function () {
      return {
        finals: cpSorted([...this.finals]),
        initials: cpSorted([...this.initials].map(i => i === "" ? "xx" : i)),
        tones: [...this.tones].sort((a, b) => a - b),
        strokes: this.strokes.map(([op, n]) => ({ op, n })),
        structures: cpSorted([...this.structures]),
        liushu: cpSorted([...this.liushu]),
        parts: cpSorted([...this.parts]),
        ids: cpSorted([...this.ids]),
        literals: cpSorted([...this.literals]),
      };
    };

    /** 反向序列化，供表单 <-> 文本框双向同步。无任何条件时输出 []。 */
    CharConstraint.prototype.toDsl = function () {
      const items = [...this.tones].sort((a, b) => a - b)
        .map(t => t === 5 ? "~5" : "~" + t);
      items.push(...this.strokes.map(([op, n]) => strokeToDsl(op, n)));
      items.push(...cpSorted([...this.structures]));
      items.push(...cpSorted([...this.liushu]));
      items.push(...cpSorted([...this.finals]).map(f => P.displayFinal(f)));
      items.push(...cpSorted([...this.initials]).map(i => i === "" ? "xx" : i));
      items.push(...cpSorted([...this.parts]));
      items.push(...cpSorted([...this.ids]));
      if (this.literals.size) {
        items.push("(" + cpSorted([...this.literals]).join("") + ")");
      }
      return "[" + items.join(",") + "]";
    };

    /* ---- 单条件分类 ---- */

    function classify(item) {
      if (!item) return null;

      if (item[0] === "~") {
        const body = item.slice(1);
        return TONE_TOKENS[body] !== undefined ? ["tones", TONE_TOKENS[body]] : null;
      }

      const cps = Array.from(item);
      const allDigit = /^\d+$/.test(item);
      if (allDigit) return ["strokes", ["eq", +item]];
      const lastCh = cps[cps.length - 1];
      if (cps.length >= 2 && STROKE_SUFFIX_LE.indexOf(lastCh) >= 0 &&
          /^\d+$/.test(item.slice(0, -1))) {
        return ["strokes", ["le", +item.slice(0, -1)]];
      }
      if (cps.length >= 2 && lastCh === "+" && /^\d+$/.test(item.slice(0, -1))) {
        return ["strokes", ["ge", +item.slice(0, -1)]];
      }

      if (STRUCTURES.indexOf(item) >= 0) return ["structures", item];
      if (LIUSHU.indexOf(item) >= 0) return ["liushu", item];
      if (IDS_OPS.has(cps[0])) return ["ids", item];

      // 声母先于韵母判：两表交集为空，顺序其实无所谓，但先判短的更自然
      const ini = P.canonicalInitial(item);
      if (ini !== null) return ["initials", ini];
      const fin = P.canonicalFinal(item);
      if (fin !== null) return ["finals", fin];

      // 裸写单字部件：语义是「该字的部首是这个字，或该字的拆字里含这个部件」，
      // 所以 [日] 既命中 明（部首日）也命中 白（拆作 ⿻？日）。
      if (cps.length === 1 && isHan(item)) return ["parts", item];

      return null;
    }

    function parseLiteralGroup(body, offset, ignored) {
      const out = new Set();
      for (const ch of body) {
        if (isHan(ch)) out.add(ch);
        else if (!IS_SPACE.test(ch) && ch !== "，" && ch !== ",") {
          ignored.push({ text: ch, offset, reason: "字面集内非汉字" });
        }
      }
      return out;
    }

    /** 解析 [] 里的内容（已去掉方括号）。 */
    function parseConstraintGroup(body, offset, ignored) {
      const acc = { finals: new Set(), initials: new Set(), tones: new Set(),
                    strokes: [], structures: new Set(), liushu: new Set(),
                    parts: new Set(), ids: new Set(), literals: new Set() };
      // 逗号与空白都是分隔符。空白单独出现时不该变成条件，所以当作分隔比
      // 「删掉再拼」更贴合意图：[a e] 显然是两个条件，而不是无法识别的 "ae"。
      for (const raw of body.split(/[,\s]+/)) {
        const item = raw.trim();
        if (!item) continue;

        if (item.length >= 2 && item[0] === "(" && item[item.length - 1] === ")") {
          for (const c of parseLiteralGroup(item.slice(1, -1), offset, ignored)) {
            acc.literals.add(c);
          }
          continue;
        }
        if (item[0] === "(" || item[item.length - 1] === ")") {
          ignored.push({ text: raw.trim(), offset, reason: "圆括号不配对" });
          continue;
        }

        const hit = classify(item);
        if (hit === null) {
          ignored.push({ text: raw.trim(), offset, reason: "无法识别" });
          continue;
        }
        const [key, value] = hit;
        if (key === "strokes") acc.strokes.push(value);
        else acc[key].add(value);
      }
      return new CharConstraint(acc);
    }

    /* ---- 顶层模式解析 ---- */

    function indexOfFrom(arr, ch, from) {
      for (let i = from; i < arr.length; i++) if (arr[i] === ch) return i;
      return -1;
    }

    function parsePattern(text) {
      const norm = normalizeSymbols(text);
      const normArr = Array.from(norm);
      const ignored = [], errors = [], elements = [];
      const n = normArr.length;
      let i = 0;

      while (i < n) {
        const ch = normArr[i];

        if (IS_SPACE.test(ch) || ch === ",") { i++; continue; }

        if (ch === "\\") {
          if (i + 1 >= n) { errors.push({ text: "\\", offset: i, reason: "转义符后无字符" }); i++; }
          else { elements.push({ kind: "lit", ch: normArr[i + 1] }); i += 2; }
          continue;
        }
        if (ch === "?") { elements.push({ kind: "any" }); i++; continue; }
        if (ch === "*") {
          if (!elements.length || elements[elements.length - 1].kind !== "star") {
            elements.push({ kind: "star" });
          }
          i++;
          continue;
        }

        if (ch === "[" || ch === "【" || ch === "〔") {
          let close = -1;
          for (const c of ["]", "】", "〕"]) {
            const p = indexOfFrom(normArr, c, i + 1);
            if (p >= 0 && (close < 0 || p < close)) close = p;
          }
          if (close < 0) { errors.push({ text: norm.slice(i), offset: i,
                                         reason: "方括号未闭合" }); break; }
          const groupOff = i + 1;
          elements.push({ kind: "slot",
                          c: parseConstraintGroup(norm.slice(groupOff, close),
                                                  groupOff, ignored) });
          i = close + 1;
          continue;
        }

        if (ch === "(" || ch === "（") {
          let close = -1;
          for (const c of [")", "）"]) {
            const p = indexOfFrom(normArr, c, i + 1);
            if (p >= 0 && (close < 0 || p < close)) close = p;
          }
          if (close < 0) { errors.push({ text: norm.slice(i), offset: i,
                                         reason: "圆括号未闭合" }); break; }
          const lits = parseLiteralGroup(norm.slice(i + 1, close), i + 1, ignored);
          if (lits.size) {
            elements.push({ kind: "slot", c: new CharConstraint({ literals: lits }) });
          }
          i = close + 1;
          continue;
        }

        if (isHan(ch) || IS_ALNUM.test(ch)) { elements.push({ kind: "lit", ch }); i++; continue; }

        ignored.push({ text: ch, offset: i, reason: "无法识别" });
        i++;
      }

      // 把 "lit"/"slot"/"any"/"star" 编译成扫描时用的紧凑形
      const els = [];
      let hasStar = false, fixedLen = 0;
      for (const el of elements) {
        // 每个字位都恰好占一个字：[]、字面、? 都算，* 不算（它长度不定）。
        // 漏掉 slot 会让 [a,an,ang][~3,~4][7-][独体,左右] 报成「匹配 0 字词」。
        if (el.kind === "slot") { els.push(["slot", el.c]); fixedLen++; }
        else if (el.kind === "lit") { els.push(["lit", el.ch]); fixedLen++; }
        else if (el.kind === "any") { els.push(["any", null]); fixedLen++; }
        else { els.push(["star", null]); hasStar = true; }
      }

      return {
        elements, ignored, errors, source: text, normalized: norm,
        compiled: { elements: els, hasStar },
        fixedLength: hasStar ? null : fixedLen,
        toDsl() {
          return elements.map(el => el.kind === "lit" ? el.ch
            : el.kind === "slot" ? el.c.toDsl()
            : el.kind === "any" ? "?" : "*").join("");
        },
      };
    }

    return { parsePattern, CharConstraint, isHan, normalizeSymbols, cpSorted };
  }

  /* ====================================================================
   * 三、词库二进制
   * ==================================================================== */

  const SECTION_NAMES = ["chars.cp", "chars.strokes", "chars.struct", "chars.liushu",
                         "chars.cfreq", "chars.radical", "chars.ids", "chars.comps",
                         "parts.cp", "syls", "buckets", "words", "defs"];

  function decodeCorpus(buf, T) {
    const bytes = new Uint8Array(buf);
    const view = new DataView(buf);
    const magic = String.fromCharCode(...bytes.subarray(0, 8));
    if (magic !== "CNNUTRI1") throw new Error("不是 cn-nutri 客户端词库（magic 不对）");
    const version = view.getUint16(8, true);
    if (version !== ENGINE_VERSION) {
      throw new Error("词库版本 " + version + " 与引擎版本 " + ENGINE_VERSION + " 不符");
    }
    const nChars = view.getUint32(12, true);
    const nWords = view.getUint32(16, true);
    const nSyls = view.getUint32(20, true);
    const nParts = view.getUint32(24, true);
    const maxLen = view.getUint32(28, true);

    let pos = 32;
    const sec = {};
    for (const name of SECTION_NAMES) {
      const n = view.getUint32(pos, true);
      sec[name] = { start: pos + 4, len: n };
      pos += 4 + align4(n);
    }

    function typed(name, Type, count) {
      const s = sec[name];
      // 段首地址是按字节给的，可能没对齐到 4，所以必须用 byteOffset 构造
      return new Type(buf, s.start, count);
    }

    const cps = typed("chars.cp", Uint32Array, nChars);
    const strokes = typed("chars.strokes", Uint8Array, nChars);
    const struct = typed("chars.struct", Uint8Array, nChars);
    const liushu = typed("chars.liushu", Uint8Array, nChars);
    const cfreq = typed("chars.cfreq", Uint32Array, nChars);
    const radical = typed("chars.radical", Uint16Array, nChars);
    const parts = typed("parts.cp", Uint32Array, nParts);

    // ids：u16 长度表 + blob，0xFFFF = 无
    const ids = new Array(nChars);
    {
      const s = sec["chars.ids"];
      const lens = new Uint16Array(buf, s.start, nChars);
      let off = s.start + 2 * nChars;
      const dec = new TextDecoder("utf-8");
      for (let i = 0; i < nChars; i++) {
        const ln = lens[i];
        if (ln === NULL16) { ids[i] = null; }
        else { ids[i] = dec.decode(bytes.subarray(off, off + ln)); off += ln; }
      }
    }

    // 部件：偏移表 + 扁平 u16
    const comps = new Array(nChars);
    {
      const s = sec["chars.comps"];
      const offs = new Uint32Array(buf, s.start, nChars + 1);
      const flat = new Uint16Array(buf, s.start + 4 * (nChars + 1), offs[nChars]);
      for (let i = 0; i < nChars; i++) {
        comps[i] = flat.subarray(offs[i], offs[i + 1]);   // 视图，不复制
      }
    }

    // 音节表：三条并列的 u8
    const sylFin = new Uint8Array(nSyls), sylIni = new Uint8Array(nSyls),
          sylTone = new Uint8Array(nSyls);
    {
      const s = sec.syls;
      for (let i = 0; i < nSyls; i++) {
        sylFin[i] = bytes[s.start + 3 * i];
        sylIni[i] = bytes[s.start + 3 * i + 1];
        sylTone[i] = bytes[s.start + 3 * i + 2];
      }
    }

    const buckets = [];
    {
      const s = sec.buckets;
      const v = new DataView(buf, s.start, s.len);
      for (let L = 0; L <= maxLen; L++) {
        buckets.push([v.getUint32(8 * L, true), v.getUint32(8 * L + 4, true)]);
      }
    }

    const wordsStart = sec.words.start;
    // 词记录长度不一（n_reads 不同），而记录里不存词长（由所在桶给出）。按词长
    // 分桶只能顺序走，但要随机取第 wi 条（结果页只渲染命中的那几条），所以先扫
    // 一遍记下每条记录的字节偏移和词长。
    const wordOff = new Uint32Array(nWords);
    const wordLen = new Uint8Array(nWords);
    {
      let o = wordsStart, wi = 0;
      for (let L = 0; L <= maxLen; L++) {
        const count = buckets[L][1];
        for (let k = 0; k < count; k++) {
          wordOff[wi] = o;
          wordLen[wi] = L;
          wi++;
          o += 7 + 2 * L * (1 + bytes[o + 6]);
        }
      }
    }

    // 释义：u16 长度表，按需解码（只渲染命中的那几条）。前缀和让随机取是 O(1)。
    const defs = { lens: null, start: 0, cum: null, cache: new Array(nWords) };

    // 码位 -> 字符表下标
    const charIndex = new Map();
    for (let i = 0; i < nChars; i++) charIndex.set(cps[i], i);

    // 部件码位 -> 部件表下标
    const partIndex = new Map();
    for (let i = 0; i < nParts; i++) partIndex.set(parts[i], i);

    return {
      nChars, nWords, nSyls, nParts, maxLen,
      buf, bytes, view, sec, wordsStart, wordOff, wordLen, buckets,
      cps, strokes, struct, liushu, cfreq, radical, comps, parts, ids,
      sylFin, sylIni, sylTone, charIndex, partIndex, defs, tables: T,
      //: 词长 L 的第 wi 条（桶内序号）在 words 段里的绝对字节偏移
      bucketStart(L) { return buckets[L][0]; },

      charAt(idx) { return String.fromCodePoint(cps[idx]); },
      charText(charsOff, L) {
        let s = "";
        for (let i = 0; i < L; i++) s += String.fromCodePoint(cps[this.u16(charsOff + 2 * i)]);
        return s;
      },
      u16(off) { return this.bytes[off] | (this.bytes[off + 1] << 8); },
      u32(off) { return (this.bytes[off] | (this.bytes[off + 1] << 8) |
                         (this.bytes[off + 2] << 16) | (this.bytes[off + 3] << 24)) >>> 0; },
      definition(wi) {
        if (this.defs.lens === null) {
          const s = sec.defs;
          this.defs.start = s.start + 2 * nWords;
          this.defs.lens = new Uint16Array(buf, s.start, nWords);
          this.defs.cum = new Uint32Array(nWords + 1);
          let acc = 0;
          for (let i = 0; i < nWords; i++) {
            acc += this.defs.lens[i] === NULL16 ? 0 : this.defs.lens[i];
            this.defs.cum[i + 1] = acc;
          }
        }
        const cached = this.defs.cache[wi];
        if (cached !== undefined) return cached;
        const ln = this.defs.lens[wi];
        const out = ln === NULL16 ? ""
          : new TextDecoder("utf-8").decode(bytes.subarray(
              this.defs.start + this.defs.cum[wi],
              this.defs.start + this.defs.cum[wi] + ln));
        this.defs.cache[wi] = out;
        return out;
      },
    };
  }

  /* ====================================================================
   * 四、匹配（matcher.py 的移植）
   * ==================================================================== */

  /** 把一个字的约束压成扫描时不再碰对象属性的扁平形。 */
  function prepareConstraint(c, ctx) {
    const finIdx = new Set(), iniIdx = new Set();
    for (const f of c.finals) {
      const i = ctx.finalIndex.get(f);
      if (i !== undefined) finIdx.add(i);
    }
    for (const s of c.initials) {
      const i = ctx.initialIndex.get(s);
      if (i !== undefined) iniIdx.add(i);
    }
    const structIds = new Set(), liushuIds = new Set(), partIds = new Set();
    for (const s of c.structures) {
      const i = ctx.structIndex.get(s);
      if (i !== undefined) structIds.add(i);
    }
    for (const s of c.liushu) {
      const i = ctx.liushuIndex.get(s);
      if (i !== undefined) liushuIds.add(i);
    }
    for (const p of c.parts) {
      const i = ctx.corpus.partIndex.get(p.codePointAt(0));
      if (i !== undefined) partIds.add(i);
    }
    const literals = new Set();
    for (const ch of c.literals) literals.add(ch.codePointAt(0));

    return {
      empty: c.isEmpty(),
      literals,
      finIdx, iniIdx, tones: c.tones,
      strokeSet: c.strokeSet(),
      structIds, liushuIds, partIds,
      ids: c.ids,
      // Python 里判「有没有这一类条件」用的是集合非空，而不是「有没有命中任何值」。
      // 分开记是必须的：结构名若都不在表里，仍然要强制走「结构数据缺失 -> 判不了」
      // 这条路，而不是变成「无条件」。
      hasFin: c.finals.size > 0, hasIni: c.initials.size > 0,
      hasTone: c.tones.size > 0, hasStruct: c.structures.size > 0,
      hasLiushu: c.liushu.size > 0, hasParts: c.parts.size > 0, hasIds: c.ids.size > 0,
    };
  }

  const TRUE_VERDICT = 1, FALSE_VERDICT = 0, UNKNOWN_VERDICT = -1;

  /**
   * 一个字是否满足约束。TRUE/FALSE 是判定，UNKNOWN 表示数据缺失。
   * 对应 matcher.match_prepared()。
   */
  function matchPrepared(p, charIdx, sylIdx, ctx) {
    const C = ctx.corpus;
    if (p.empty) return TRUE_VERDICT;

    if (p.literals.size) {
      // charIdx 可能是 -1（词里出现了字符表外的字，理论上不会发生）
      if (charIdx < 0 || !p.literals.has(C.cps[charIdx])) return FALSE_VERDICT;
    }

    if (p.hasIni || p.hasFin || p.hasTone) {
      if (sylIdx < 0) return UNKNOWN_VERDICT;
      if (p.hasIni && !p.iniIdx.has(C.sylIni[sylIdx])) return FALSE_VERDICT;
      if (p.hasFin && !p.finIdx.has(C.sylFin[sylIdx])) return FALSE_VERDICT;
      if (p.hasTone && !p.tones.has(C.sylTone[sylIdx])) return FALSE_VERDICT;
    }

    if (charIdx < 0) {
      // 无字形数据：只要还有字形类条件就无法判定
      if (p.strokeSet !== null || p.hasStruct || p.hasLiushu || p.hasParts || p.hasIds) {
        return UNKNOWN_VERDICT;
      }
      return TRUE_VERDICT;
    }

    if (p.strokeSet !== null) {
      const st = C.strokes[charIdx];
      if (st === 0) return UNKNOWN_VERDICT;              // 0 = 未知
      if (!p.strokeSet.has(st)) return FALSE_VERDICT;
    }
    if (p.hasStruct) {
      const sid = C.struct[charIdx];
      if (sid === NULL8) return UNKNOWN_VERDICT;
      if (!p.structIds.has(sid)) return FALSE_VERDICT;
    }
    if (p.hasLiushu) {
      const lid = C.liushu[charIdx];
      if (lid === NULL8) return UNKNOWN_VERDICT;
      if (!p.liushuIds.has(lid)) return FALSE_VERDICT;
    }
    if (p.hasIds) {
      const s = C.ids[charIdx];
      if (s === null || !p.ids.has(s)) return FALSE_VERDICT;
    }

    // 部件语义是并集：以该字为部首，或该字的拆字里含这个部件。
    // 所以 [日] 同时命中 明（部首日）与 白（拆作 ⿱丿日）。
    if (p.hasParts) {
      const rad = C.radical[charIdx];
      let ok = rad !== NULL16 && p.partIds.has(rad);
      if (!ok) {
        const cs = C.comps[charIdx];
        for (let i = 0; i < cs.length; i++) {
          if (p.partIds.has(cs[i])) { ok = true; break; }
        }
      }
      if (!ok) return FALSE_VERDICT;
    }

    return TRUE_VERDICT;
  }

  /** 词记录的一个可复用视图，避免为 36.8 万条各建一个对象。 */
  function makeWordRef() {
    return { off: 0, L: 0, freq: 0, flags: 0, tier: 0, nReads: 0, charsOff: 0, readsOff: 0 };
  }

  function loadWordRef(C, wi, W) {
    const o = C.wordOff[wi];
    const b = C.bytes;
    W.off = o;
    W.L = C.wordLen[wi];
    W.freq = (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;
    W.flags = b[o + 4];
    W.tier = b[o + 5];
    W.nReads = b[o + 6];
    W.charsOff = o + 7;
    W.readsOff = W.charsOff + 2 * W.L;
    return W;
  }

  /* ====================================================================
   * 五、排序（rank.py 的移植）
   * ==================================================================== */

  function compareWords(C, aCharsOff, aL, bCharsOff, bL) {
    const n = Math.min(aL, bL);
    for (let i = 0; i < n; i++) {
      const d = C.cps[C.u16(aCharsOff + 2 * i)] - C.cps[C.u16(bCharsOff + 2 * i)];
      if (d) return d;
    }
    return aL - bL;
  }

  /* ====================================================================
   * 六、引擎
   * ==================================================================== */

  function Engine(tables, corpus) {
    this.T = tables;
    this.C = corpus;
    this.P = makePinyin(tables.pinyin);
    this.Q = makeQuery(tables.query, this.P);
    this.sortModes = tables.rank.sort_modes;
    this.maxResults = tables.search.max_results;
    this.meta = tables.meta;
    this.corpusInfo = tables.corpus;

    const pc = corpus;
    this.ctx = {
      corpus: pc,
      finalIndex: new Map(this.P.finals.map((f, i) => [f, i])),
      initialIndex: new Map(pc.tables.corpus.initials.map((s, i) => [s, i])),
      structIndex: new Map(pc.tables.corpus.structures.map((s, i) => [s, i])),
      liushuIndex: new Map(pc.tables.corpus.liushu_names.map((s, i) => [s, i])),
    };
    this._W = makeWordRef();
  }

  Engine.prototype.parse = function (q) {
    const p = this.Q.parsePattern(q || "");
    return {
      dsl: p.toDsl(),
      fixed_length: p.fixedLength,
      elements: p.elements.map(el => el.kind === "lit" ? { kind: "literal", ch: el.ch }
        : el.kind === "slot" ? { kind: "slot", c: el.c.toDict() }
        : el.kind === "any" ? { kind: "any" } : { kind: "star" }),
      ignored: p.ignored,
      errors: p.errors,
    };
  };

  /**
   * 与 search.py 的 search() 同形。排序只需前 offset+limit 条，但排序键的末位
   * 是唯一的词，全序，所以「全排再切片」与 Python 的 nsmallest 结果逐条相同
   * （heapq.nsmallest 文档里就写明等价于 sorted(...)[:n]）。
   */
  Engine.prototype.search = function (query, opts) {
    opts = opts || {};
    const t0 = (global.performance || Date).now();
    const C = this.C;
    const p = this.Q.parsePattern(
      typeof query === "string" ? query : (query && query.dsl) || "");

    let sort = opts.sort || "freq";
    if (this.sortModes.indexOf(sort) < 0) sort = "freq";
    const limit = opts.limit === undefined ? 50 : opts.limit;
    const offset = opts.offset || 0;
    const idiomOnly = !!opts.idiomOnly;

    const out = {
      dsl: p.toDsl(), total: 0, offset, limit, sort,
      elapsed_ms: 0, truncated: false, max_results: this.maxResults,
      error: null, ignored: p.ignored, errors: p.errors,
      excluded_missing: 0, missing_chars: "", scanned: 0, results: [],
    };
    if (p.errors.length) {
      out.error = p.errors[0].reason;
      out.elapsed_ms = (global.performance || Date).now() - t0;
      return out;
    }

    const ctx = this.ctx, compiled = p.compiled;
    const els = compiled.elements, hasStar = compiled.hasStar;

    // 编译约束：只做一次，别在 36.8 万次扫描里反复建 Set。
    // 同一个 Pattern 可能被搜两次（表单和文本框各一次），所以标记一下。
    if (!compiled.prepared) {
      for (const el of els) if (el[0] === "slot") el[1] = prepareConstraint(el[1], ctx);
      compiled.prepared = true;
    }

    const unknown = new Set();           // 本词内因数据缺失判不了的字（码位）
    const missing = new Set();           // 全查询累计
    let excluded = 0, scanned = 0;
    const hits = [];

    const candidateLens = hasStar ? null : els.length;

    const scanFrom = candidateLens === null ? 1 : candidateLens;
    const scanTo = candidateLens === null ? C.maxLen : candidateLens;

    const W = this._W;
    for (let L = scanFrom; L <= scanTo; L++) {
      if (L > C.maxLen) break;
      const start = C.buckets[L][0], count = C.buckets[L][1];
      if (!count) continue;
      W.L = L;
      for (let k = 0; k < count; k++) {
        const wi = start + k;
        loadWordRef(C, wi, W);
        if (idiomOnly && !(W.flags & 1)) continue;
        scanned++;

        const n = L;
        if (!hasStar && els.length !== n) continue;   // 桶保证了不可能，留作断言

        unknown.clear();
        const nReads = W.nReads;
        const readCount = nReads || 1;
        let hit = null;
        for (let ri = 0; ri < readCount; ri++) {
          const sylBase = W.readsOff + 2 * L * ri;
          const spans = hasStar
            ? globMatch(els, C, W, sylBase, n, nReads ? ri : -1, ctx, unknown)
            : scanFixed(els, C, W, sylBase, n, nReads ? ri : -1, ctx, unknown);
          if (spans !== null) { hit = { wi, ri: nReads ? ri : -1, spans }; break; }
        }

        if (hit !== null) { hits.push(hit); continue; }
        if (unknown.size) {
          excluded++;
          for (const cp of unknown) missing.add(cp);
        }
      }
    }

    out.scanned = scanned;
    out.excluded_missing = excluded;
    out.missing_chars = [...missing].sort((a, b) => a - b).slice(0, 40)
      .map(cp => String.fromCodePoint(cp)).join("");
    out.total = hits.length;
    out.truncated = hits.length > this.maxResults;

    const want = Math.min(offset + limit, this.maxResults);
    const sorted = this._sortHits(hits, sort);
    out.results = sorted.slice(offset, want).map(h => this._matchJson(h, sort));
    out.elapsed_ms = Math.round(((global.performance || Date).now() - t0) * 10) / 10;
    return out;
  };

  /** 定长直路：无 * 时不需要回溯。 */
  function scanFixed(els, C, W, sylBase, n, ri, ctx, unknown) {
    const spans = [];
    for (let i = 0; i < els.length; i++) {
      const kind = els[i][0], payload = els[i][1];
      const ci = C.u16(W.charsOff + 2 * i);
      if (kind === "slot") {
        const si = ri < 0 ? -1 : C.u16(sylBase + 2 * i);
        const verdict = matchPrepared(payload, ci, si, ctx);
        if (verdict === UNKNOWN_VERDICT) { unknown.add(C.cps[ci]); return null; }
        if (verdict === FALSE_VERDICT) return null;
      } else if (kind === "lit") {
        if (String.fromCodePoint(C.cps[ci]) !== payload) return null;
      }
      spans.push(i);
    }
    return spans;
  }

  /** 含 * 时的记忆化回溯。成功返回每个非 * 元素命中的字位。 */
  function globMatch(els, C, W, sylBase, n, ri, ctx, unknown) {
    const en = els.length;
    const memo = new Map();

    function rec(ei, wi) {
      const key = ei * (n + 1) + wi;
      const cached = memo.get(key);
      if (cached !== undefined) return cached;

      if (ei === en) {
        const res = wi === n ? [] : null;
        memo.set(key, res);
        return res;
      }

      const kind = els[ei][0], payload = els[ei][1];
      let out = null;

      if (kind === "star") {
        for (let k = wi; k <= n; k++) {
          const tail = rec(ei + 1, k);
          if (tail !== null) { out = tail; break; }
        }
      } else if (wi >= n) {
        out = null;
      } else {
        const ci = C.u16(W.charsOff + 2 * wi);
        let ok = false;
        if (kind === "any") {
          ok = true;
        } else if (kind === "lit") {
          ok = String.fromCodePoint(C.cps[ci]) === payload;
        } else {
          const si = ri < 0 ? -1 : C.u16(sylBase + 2 * wi);
          const verdict = matchPrepared(payload, ci, si, ctx);
          if (verdict === UNKNOWN_VERDICT) unknown.add(C.cps[ci]);
          else if (verdict === TRUE_VERDICT) ok = true;
        }
        if (ok) {
          const tail = rec(ei + 1, wi + 1);
          if (tail !== null) out = [wi, ...tail];
        }
      }

      memo.set(key, out);
      return out;
    }

    return rec(0, 0);
  }

  Engine.prototype._hitKey = function (h, sort) {
    const C = this.C, W = this._W;
    loadWordRef(C, h.wi, W);
    // sort_aux 不存盘，由语料字频现算——build_client_data.py --check 验过
    // 它能逐条复原，所以这里和 Python 的 w.sort_aux 是同一个数。
    let aux = 0;
    const L = W.L;
    for (let i = 0; i < L; i++) aux += C.cfreq[C.u16(W.charsOff + 2 * i)];
    h.tier = W.tier; h.freq = W.freq; h.aux = aux; h.L = L;
    h.charsOff = W.charsOff;
    return h;
  };

  Engine.prototype._sortHits = function (hits, sort) {
    const C = this.C;
    for (const h of hits) this._hitKey(h, sort);

    let extra = null;
    if (sort === "strokes") {
      for (const h of hits) {
        let total = 0;
        for (let i = 0; i < h.L; i++) total += C.strokes[C.u16(h.charsOff + 2 * i)];
        h.total = total;
      }
    } else if (sort === "pinyin") {
      for (const h of hits) h.py = this._pinyinDisplay(h.wi, h.ri);
    } else if (sort === "len") {
      // 词长就是 h.L，无需额外字段
    }

    const cmpWord = (a, b) => compareWords(C, a.charsOff, a.L, b.charsOff, b.L);
    let cmp;
    if (sort === "len") {
      cmp = (a, b) => (a.tier - b.tier) || (a.L - b.L) || (b.freq - a.freq) ||
                      (b.aux - a.aux) || cmpWord(a, b);
    } else if (sort === "strokes") {
      cmp = (a, b) => (a.total - b.total) || (a.tier - b.tier) ||
                      (b.freq - a.freq) || cmpWord(a, b);
    } else if (sort === "pinyin") {
      // 拼音串全是 BMP（声调符号 U+0100–U+01FF），JS 的 < 与 Python 的字符串
      // 比较在这里结果一致
      cmp = (a, b) => (a.py < b.py ? -1 : a.py > b.py ? 1 : 0) ||
                      (a.tier - b.tier) || (b.freq - a.freq) || cmpWord(a, b);
    } else {
      cmp = (a, b) => (a.tier - b.tier) || (b.freq - a.freq) ||
                      (b.aux - a.aux) || cmpWord(a, b);
    }
    return hits.sort(cmp);
  };

  Engine.prototype._pinyinDisplay = function (wi, ri) {
    const C = this.C, W = loadWordRef(C, wi, this._W);
    const L = W.L;
    if (ri < 0 || ri >= W.nReads) return "";
    const base = W.readsOff + 2 * L * ri;
    const out = [];
    const ci = C.tables.corpus.initials, cf = C.tables.corpus.finals;
    for (let i = 0; i < L; i++) {
      const s = C.u16(base + 2 * i);
      // 音节表存的下标进的是**语料侧**那张声母表——它第 0 个是零声母 ""，
      // 比 tables.pinyin.initials 多一项，拿错表就等于每个字都错一位。
      out.push(this.P.formatPinyin(ci[C.sylIni[s]], cf[C.sylFin[s]],
                                   C.sylTone[s]));
    }
    return out.join(" ");
  };

  Engine.prototype._matchJson = function (h, sort) {
    const C = this.C, T = this.T;
    const W = loadWordRef(C, h.wi, this._W);
    const L = W.L;
    const word = C.charText(W.charsOff, L);
    const chars = [];
    for (let i = 0; i < L; i++) {
      const ci = C.u16(W.charsOff + 2 * i);
      chars.push({
        ch: C.charAt(ci),
        strokes: C.strokes[ci] || null,
        structure: C.struct[ci] === NULL8 ? null : T.corpus.structures[C.struct[ci]],
        // 部首存的是**部件表**下标，不是字符表下标
        radical: C.radical[ci] === NULL16
          ? null : String.fromCodePoint(C.parts[C.radical[ci]]),
        liushu: C.liushu[ci] === NULL8 ? null : T.corpus.liushu_names[C.liushu[ci]],
        ids: C.ids[ci],
      });
    }
    const pinyin = [], sources = [];
    for (let r = 0; r < W.nReads; r++) {
      pinyin.push(this._pinyinDisplay(h.wi, r));
      sources.push((W.flags & (1 << (2 + r))) ? "inferred" : "cedict");
    }
    return {
      word, word_trad: null, length: L, freq: W.freq, tier: W.tier,
      is_idiom: !!(W.flags & 1), definition: C.definition(h.wi),
      pinyin, read_index: h.ri, read_sources: sources,
      spans: h.spans, chars,
    };
  };

  /* ====================================================================
   * 七、装配
   * ==================================================================== */

  function fromBuffers(tables, corpusBuffer) {
    const normalized = normalizeTables(tables);
    const corpus = decodeCorpus(corpusBuffer, normalized);
    return new Engine(normalized, corpus);
  }

  /** tables.json 里 finals/initials 是并列的数组，补一份语料侧的下标表。 */
  function normalizeTables(t) {
    if (!t.corpus.finals || !t.corpus.initials) {
      t = JSON.parse(JSON.stringify(t));
      t.corpus.finals = t.pinyin.finals;
      t.corpus.initials = [""].concat(t.pinyin.initials);
    }
    return t;
  }

  function load(baseUrl, onProgress) {
    const base = (baseUrl || "").replace(/\/$/, "");
    const p = (u, how) => fetch(u).then(r => {
      if (!r.ok) throw new Error(u + " -> HTTP " + r.status);
      return r[how]();
    });
    return Promise.all([
      p(base + "/data/tables.json", "json"),
      p(base + "/data/corpus.bin", "arrayBuffer"),
    ]).then(([tables, buf]) => {
      if (onProgress) onProgress(buf.byteLength);
      return fromBuffers(tables, buf);
    });
  }

  global.CnNutri = {
    VERSION: ENGINE_VERSION,
    load,
    fromBuffers,
    normalizeTables,
    decodeCorpus,
    _internal: { makePinyin, makeQuery },
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
