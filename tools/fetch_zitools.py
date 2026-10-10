#!/usr/bin/env python3
"""抓取字统网（zi.tools）的紧凑 IDS（表意文字描述序列）与难输入部件表。

用紧凑接口 ``/api/ids/lookupids/<字>``（400B～3KB），而不是 ``/api/zi/<字>``
（300～550KB）——后者 7,533 字要 655MB，属「规模过大」，只在显式加
``--with-zi-full`` 时才抓（用于取异体字/字源）。

注意：``web.zi.tools`` 在本机因 SNI 不匹配握手失败，必须用裸域 ``zi.tools``。

用法
----
    python -I tools/fetch_zitools.py --chars data_raw/corpus_chars.txt \
                                     --out data_raw/zitools [--delay 0.2]
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

IDS_URL = "https://zi.tools/api/ids/lookupids/"
FULL_URL = "https://zi.tools/api/zi/"
DIFFICULT_URL = "https://zi.tools/api/lookup/difficult_components"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: IDS 算子
OPERATORS = frozenset("⿰⿱⿲⿳⿴⿵⿶⿷⿸⿹⿺⿻⿼⿽⿾⿿")


def fetch_json(url: str, retries: int = 3, timeout: int = 25):
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError, gzip.BadGzipFile) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def clean_ids(ids: str) -> str:
    """去掉字统网用来标记叶节点的尾点：``⿰弓.长.`` -> ``⿰弓长``。"""
    return ids.replace(".", "").strip()


def extract_ids(payload: dict, ch: str) -> str | None:
    """从 lookupids 响应里取最规范的一版 IDS（lv1 优先）。"""
    node = payload.get(ch)
    if not isinstance(node, dict):
        return None
    for lv in ("lv1", "lv2", "lv0", "lv3"):
        sub = node.get(lv)
        if isinstance(sub, dict):
            lst = sub.get("ids_list") or []
            if lst and isinstance(lst[0], str):
                return clean_ids(lst[0])
    return None


def ids_symbols(ids: str) -> list[str]:
    """列出一条 IDS 里的所有符号（含算子），用于调试。"""
    return [c for c in ids]


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取字统网紧凑 IDS")
    ap.add_argument("--chars", default="data_raw/corpus_chars.txt")
    ap.add_argument("--out", default="data_raw/zitools")
    ap.add_argument("--delay", type=float, default=0.05)
    ap.add_argument("--workers", type=int, default=6,
                    help="并发数。单次延迟在 0.2s~18s 间剧烈波动（服务端按需算 IDS），"
                         "串行会拖到十几小时，故用适度并发")
    ap.add_argument("--with-zi-full", action="store_true",
                    help="额外抓 /api/zi/ 全量（异体字/字源，约 655MB）")
    ap.add_argument("--probe", default=None, help="只探测这几个字")
    args = ap.parse_args()

    if args.probe:
        for ch in args.probe:
            payload = fetch_json(IDS_URL + urllib.parse.quote(ch))
            print(f"{ch}  ids={extract_ids(payload, ch)!r}")
        return 0

    os.makedirs(args.out, exist_ok=True)

    # 难输入部件表：一次抓完，随构建产物本地化，供前端部件面板渲染 SVG
    diff_path = os.path.join(args.out, "_difficult_components.json")
    if not os.path.exists(diff_path):
        data = fetch_json(DIFFICULT_URL)
        with open(diff_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        print(f"难输入部件表已存 {diff_path}（{os.path.getsize(diff_path)} 字节）")

    with open(args.chars, encoding="utf-8") as fh:
        chars = {c for c in fh.read() if not c.isspace()}

    # 常用字优先：基本区（U+4E00–U+9FFF）先抓完，扩展区的罕用字排后面。
    # 这样即使中途停下，拿到手的也是最有用的那部分。
    def order_key(c: str) -> tuple[int, int]:
        cp = ord(c)
        return (0 if 0x4E00 <= cp <= 0x9FFF else 1, cp)

    def cache_of(ch: str) -> str:
        return os.path.join(args.out, f"u{ord(ch):05X}.json")

    todo = sorted((c for c in chars if not os.path.exists(cache_of(c))),
                  key=order_key)
    print(f"共 {len(chars)} 字，已缓存 {len(chars) - len(todo)}，待抓 {len(todo)}"
          f"（{args.workers} 并发）", flush=True)
    if not todo:
        return 0

    t0 = time.time()
    done = 0
    missing: list[str] = []
    lock = threading.Lock()

    def work(ch: str) -> tuple[str, str | None]:
        ids = extract_ids(fetch_json(IDS_URL + urllib.parse.quote(ch)), ch)
        time.sleep(args.delay)
        return ch, ids

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, ch): ch for ch in todo}
        for fut in as_completed(futures):
            ch = futures[fut]
            try:
                _, ids = fut.result()
            except Exception as exc:
                print(f"  !! {ch}: {exc}", flush=True)
                missing.append(ch)
                continue
            if ids is None:
                missing.append(ch)
            with open(cache_of(ch), "w", encoding="utf-8") as fh:
                json.dump({"char": ch, "ids": ids}, fh, ensure_ascii=False)
            with lock:
                done += 1
                if done % 200 == 0 or done == len(todo):
                    rate = done / max(time.time() - t0, 1e-6)
                    eta = (len(todo) - done) / max(rate, 1e-6)
                    print(f"  {done}/{len(todo)}  {rate:.2f} 字/秒  "
                          f"剩余约 {eta/60:.1f} 分钟  无 IDS {len(missing)}",
                          flush=True)

    print(f"完成。无 IDS 的字 {len(missing)} 个，前 20：{''.join(missing[:20])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
