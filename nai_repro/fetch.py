#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
带断点续传的下载器（走本地代理）。

    python fetch.py <url> <输出路径>
"""
import os
import sys
import time
import urllib.request

PROXY = os.environ.get("FETCH_PROXY", "http://127.0.0.1:7890")


def opener():
    h = {"http": PROXY, "https": PROXY} if PROXY else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(h))


def download(url, out, quiet=False):
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    done = os.path.getsize(out) if os.path.isfile(out) else 0

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    if done:
        req.add_header("Range", f"bytes={done}-")

    try:
        resp = opener().open(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 416:                       # 已经下完
            print(f"已完成: {out}")
            return out
        raise

    total = None
    cr = resp.headers.get("Content-Length")
    if cr:
        total = int(cr) + done
    mode = "ab" if done else "wb"
    t0 = time.time()
    last = t0
    got = done
    with open(out, mode) as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            now = time.time()
            if not quiet and now - last > 1.0:
                last = now
                sp = (got - done) / max(0.001, now - t0) / 1e6
                if total:
                    pct = 100 * got / total
                    eta = (total - got) / max(0.001, (got - done) / (now - t0))
                    print(f"  {pct:5.1f}%  {got/1e9:6.2f}/{total/1e9:.2f} GB  "
                          f"{sp:5.1f} MB/s  剩 {eta/60:4.1f} 分", flush=True)
                else:
                    print(f"  {got/1e9:6.2f} GB  {sp:5.1f} MB/s", flush=True)
    dt = time.time() - t0
    print(f"完成: {out}  {got/1e9:.2f} GB  用时 {dt/60:.1f} 分  "
          f"均速 {(got-done)/max(0.001,dt)/1e6:.1f} MB/s")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    download(sys.argv[1], sys.argv[2])
