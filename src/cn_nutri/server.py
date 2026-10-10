"""本地 Web 后端（标准库 ThreadingHTTPServer）。

    python -m cn_nutri.server [--port 8000]

与 CLI 共用 ``search()`` 这一条查询路径，所以网页与命令行的行为不会漂移。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .components import load_components
from .corpus import DEFAULT_DB, load_corpus
from .pinyin import format_pinyin
from .query import LIUSHU_NAMES, STRUCTURES, parse_pattern
from .rank import SORT_MODES
from .search import MAX_RESULTS, search

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# 字体后缀在各平台的 mimetypes 库里不一定有，缺了会退回 octet-stream
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("font/otf", ".otf")


def _elements_json(p) -> list[dict]:
    """模式的元素序列，供前端逐字表单回填。``*``/``?`` 表单表达不了，标出来。"""
    from .query import AnyChar, Literal, Slot, Star
    out = []
    for el in p.elements:
        if isinstance(el, Literal):
            out.append({"kind": "literal", "ch": el.ch})
        elif isinstance(el, Slot):
            out.append({"kind": "slot", "c": el.constraint.to_dict()})
        elif isinstance(el, AnyChar):
            out.append({"kind": "any"})
        elif isinstance(el, Star):
            out.append({"kind": "star"})
    return out


def _issues_json(items) -> list[dict]:
    """被忽略项与错误项同形：原文、偏移、原因。原因也下发——前端要悬停显示
    「为什么这句被扔了」，client 引擎同样会带这个字段，两边得一致。"""
    return [{"text": i.text, "offset": i.offset, "reason": i.reason} for i in items]


def parse_payload(p) -> dict:
    """``/api/parse`` 的响应体。抽成函数是为了让 tests/test_client_engine.py 能
    拿同一个定义去要求 JS 引擎——测试和接口各写一份的话，两份都会慢慢跑偏。"""
    return {
        "dsl": p.to_dsl(),
        "fixed_length": p.fixed_length(),
        "elements": _elements_json(p),
        "ignored": _issues_json(p.ignored),
        "errors": _issues_json(p.errors),
    }


def search_payload(res, corpus) -> dict:
    """``/api/search`` 的响应体，同上。"""
    return {
        "dsl": res.pattern.to_dsl(),
        "total": res.total,
        "offset": res.offset,
        "limit": res.limit,
        "sort": res.sort_mode,
        "elapsed_ms": round(res.elapsed_ms, 1),
        "truncated": res.truncated,
        "max_results": MAX_RESULTS,
        "error": res.error,
        "ignored": _issues_json(res.ignored),
        "errors": _issues_json(res.errors),
        "excluded_missing": res.stats.excluded_missing,
        "missing_chars": "".join(sorted(res.stats.missing_chars)[:40]),
        "scanned": res.stats.scanned,
        "results": [_match_json(m, corpus) for m in res.matches],
    }


def _match_json(m, corpus) -> dict:
    w = m.word
    display = list(w.pinyin_display)
    src = list(w.read_sources)
    return {
        "word": w.word,
        "word_trad": None,
        "length": len(w.word),
        "freq": w.freq,
        "tier": w.tier,
        "is_idiom": w.is_idiom,
        "definition": w.definition,
        "pinyin": display,
        "read_index": m.read_index,
        "read_sources": src,
        "spans": m.spans,
        "chars": [
            {
                "ch": c,
                "strokes": (corpus.chars[c].strokes if c in corpus.chars else None),
                "structure": (corpus.chars[c].structure if c in corpus.chars else None),
                "radical": (corpus.chars[c].radical if c in corpus.chars else None),
                "liushu": (corpus.chars[c].liushu if c in corpus.chars else None),
                "ids": (corpus.chars[c].ids if c in corpus.chars else None),
            }
            for c in w.word
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "cn_nutri"
    corpus = None            # 由 main() 注入
    components: dict = {}

    def log_message(self, fmt, *args):      # 静音默认的逐请求日志
        pass

    # ---- helpers -------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ---- routes --------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route, qs = parsed.path, parse_qs(parsed.query)

        if route == "/api/meta":
            # structures / liushu 是**查询侧**的词汇表（含「独体」这类别名），
            # 不是语料里那串原值——前端表单要的就是这一份，客户端引擎从
            # tables.json 的 query 段取同一份数据。
            self._json({"meta": self.corpus.meta,
                        "n_words": len(self.corpus.all_words),
                        "n_chars": len(self.corpus.chars),
                        "structures": list(STRUCTURES),
                        "liushu": list(LIUSHU_NAMES),
                        "sort_modes": list(SORT_MODES),
                        "max_results": MAX_RESULTS})
            return

        if route == "/api/components":
            self._json(self.components)
            return

        if route == "/api/char":
            ch = (qs.get("ch") or [""])[0]
            info = self.corpus.chars.get(ch)
            if info is None:
                self._json({"ch": ch, "known": False})
                return
            self._json({
                "ch": ch, "known": True,
                "strokes": info.strokes, "radical": info.radical,
                "structure": info.structure, "liushu": info.liushu,
                "ids": info.ids,
                "readings": [format_pinyin(s.initial, s.final, s.tone)
                             for s in info.readings],
            })
            return

        if route == "/api/parse":
            q = (qs.get("q") or [""])[0]
            p = parse_pattern(q)
            self._json({
                "dsl": p.to_dsl(),
                "fixed_length": p.fixed_length(),
                "elements": _elements_json(p),
                "ignored": [{"text": i.text, "offset": i.offset} for i in p.ignored],
                "errors": [{"text": e.text, "offset": e.offset, "reason": e.reason}
                           for e in p.errors],
            })
            return

        if route == "/api/search":
            q = (qs.get("q") or [""])[0]
            try:
                limit = max(1, min(int((qs.get("limit") or ["20"])[0]), 200))
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
            except ValueError:
                limit, offset = 20, 0
            sort = (qs.get("sort") or ["freq"])[0]
            idiom = (qs.get("idiom") or ["0"])[0] not in ("0", "", "false")

            res = search(self.corpus, q, limit=limit, offset=offset,
                         sort=sort, idiom_only=idiom)
            self._json({
                "dsl": res.pattern.to_dsl(),
                "total": res.total,
                "offset": res.offset,
                "limit": res.limit,
                "sort": res.sort_mode,
                "elapsed_ms": round(res.elapsed_ms, 1),
                "truncated": res.truncated,
                "max_results": MAX_RESULTS,
                "error": res.error,
                "ignored": [{"text": i.text, "offset": i.offset}
                            for i in res.ignored],
                "errors": [{"text": e.text, "offset": e.offset, "reason": e.reason}
                           for e in res.errors],
                "excluded_missing": res.stats.excluded_missing,
                "missing_chars": "".join(sorted(res.stats.missing_chars)[:40]),
                "scanned": res.stats.scanned,
                "results": [_match_json(m, self.corpus) for m in res.matches],
            })
            return

        if route.startswith("/api/"):
            self._json({"error": "unknown endpoint"}, 404)
            return

        self._static(unquote(route))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="cn_nutri 本地 Web 界面")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args(argv)

    Handler.corpus = load_corpus(args.db)
    Handler.components = load_components()
    print(f"词库：{Handler.corpus}")
    n_parts = len({c for g in Handler.components["groups"] for c in g["chars"]})
    have_font = os.path.isfile(os.path.join(WEB_DIR, "fonts", "parts.css"))
    print(f"部件面板：{len(Handler.components['groups'])} 组 / {n_parts} 个部件"
          f"{'' if have_font else '（缺内置字体，生僻部件会显示成豆腐块）'}")

    port = args.port
    for _ in range(20):                      # 端口被占就顺延
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError as exc:
            if exc.errno in (socket.errno.EADDRINUSE, 10048):
                port += 1
            else:
                raise
    else:
        print("找不到可用端口", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{port}/"
    print(f"打开 {url}   （Ctrl-C 退出）")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
