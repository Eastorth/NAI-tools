#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAI 资源库 —— 独立项目
=======================
只读 NAI 复现工具产出的 out/ 目录，做浏览 / 查参 / 整理，不参与出图。

    python lib_server.py             # 默认 8780
    python lib_server.py --open

配置见 lib_config.json（指向 NAI 复现工具的目录）。
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "lib_config.json")

DEFAULT_CONFIG = {
    "nai_dir": r"D:\AI project\nai_repro",   # NAI 复现工具所在目录
    "port": 8780,
    "host": "127.0.0.1",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items()
                            if not k.startswith("_")})
        except Exception as e:
            print(f"[warn] 读取 {CONFIG_FILE} 失败: {e}")
    if os.environ.get("NAI_DIR"):
        cfg["nai_dir"] = os.environ["NAI_DIR"]
    return cfg


CFG = load_config()
NAI_DIR = os.path.abspath(CFG["nai_dir"])
sys.path.insert(0, NAI_DIR)

# 复用复现工具的解析代码（元数据格式是它定义的，不重复实现）
import llm_analyze  # noqa: E402
import repro  # noqa: E402

OUT = os.path.join(NAI_DIR, "out")
BATCH_ROOT = os.path.join(OUT, "batches")
CACHE_DIR = os.path.join(HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


# ---------------------------------------------------------------- 数据层

def batch_dir(bid):
    return os.path.join(BATCH_ROOT, bid)


def read_batch(bid):
    if not bid or any(c in bid for c in ("/", "\\", "..")):
        return None
    p = os.path.join(batch_dir(bid), "batch.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_batch(bid, data):
    d = batch_dir(bid)
    os.makedirs(d, exist_ok=True)
    data["id"] = bid
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(os.path.join(d, "batch.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def list_batches():
    out = []
    if not os.path.isdir(BATCH_ROOT):
        return out
    for name in sorted(os.listdir(BATCH_ROOT), reverse=True):
        b = read_batch(name)
        if not b:
            continue
        d = batch_dir(name)
        imgdir = os.path.join(d, "images")
        b["image_count"] = len([x for x in os.listdir(imgdir)
                                if x.lower().endswith(".png")]) \
            if os.path.isdir(imgdir) else 0
        b["item_count"] = len(b.get("items") or [])
        out.append(b)
    return out


def _work_of_png(fn):
    stem = os.path.splitext(fn)[0]
    stem = re.sub(r"_\d{1,10}$", "", stem)
    m = re.match(r"^(.+?)_p\d+$", stem)
    return m.group(1) if m else stem


_SIZE_CACHE = {}


def png_size(path):
    """只读 PNG 头 24 字节拿宽高——瀑布流靠它算高度，不能等图片加载。"""
    try:
        st = os.stat(path)
        hit = _SIZE_CACHE.get(path)
        if hit and hit[0] == st.st_mtime:
            return hit[1], hit[2]
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
        else:
            w = h = 0
        _SIZE_CACHE[path] = (st.st_mtime, w, h)
        return w, h
    except Exception:
        return 0, 0


def library_index(batch=None, sort="time"):
    out = []
    bids = [batch] if batch else [b["id"] for b in list_batches()]
    for bid in bids:
        b = read_batch(bid)
        if not b:
            continue
        d = batch_dir(bid)
        imgdir = os.path.join(d, "images")
        if not os.path.isdir(imgdir):
            continue
        meta = {str(i.get("png", "")).split("/")[-1]: i
                for i in (b.get("items") or [])}
        for fn in os.listdir(imgdir):
            if not fn.lower().endswith(".png"):
                continue
            p = os.path.join(imgdir, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            it = meta.get(fn, {})
            stem = os.path.splitext(fn)[0]
            w, h = png_size(p)
            out.append({
                "batch": bid,
                "batch_name": b.get("name") or bid,
                "f": fn,
                "work": it.get("name") or _work_of_png(fn),
                "seed": it.get("seed"),
                "bytes": st.st_size,
                "mtime": int(st.st_mtime),
                "w": w,
                "h": h,
                "has_request": os.path.isfile(
                    os.path.join(d, "requests", stem + ".json")),
                "has_report": os.path.isfile(
                    os.path.join(d, "meta", stem + ".report.json")),
            })
    if sort == "name":
        out.sort(key=lambda x: (x["work"], str(x["seed"])))
    elif sort == "seed":
        out.sort(key=lambda x: (x["seed"] is None, x["seed"]))
    elif sort == "batch":
        out.sort(key=lambda x: (x["batch_name"], -x["mtime"]))
    else:
        out.sort(key=lambda x: -x["mtime"])
    return out


def prompts_of(orig):
    c = orig.get("Comment", orig) if isinstance(orig, dict) else orig
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except Exception:
            c = {}
    vp = (c.get("v4_prompt") or {}).get("caption") or {}
    vn = (c.get("v4_negative_prompt") or {}).get("caption") or {}

    def chars(seq):
        r = []
        for it in (seq or []):
            if isinstance(it, dict):
                r.append({"char_caption": it.get("char_caption", ""),
                          "centers": it.get("centers")})
            else:
                r.append({"char_caption": str(it), "centers": None})
        return r

    return {"pos_base": vp.get("base_caption") or c.get("prompt") or "",
            "pos_chars": chars(vp.get("char_captions")),
            "neg_base": vn.get("base_caption") or c.get("uc") or "",
            "neg_chars": chars(vn.get("char_captions"))}


def _llm_cache_for(pr):
    """查一下这张图的提示词有没有已缓存的 LLM 分析。"""
    res = {}
    try:
        slots = llm_analyze.slots_from_prompts(pr)
        if not slots:
            return res
    except Exception:
        return res
    for mode in ("full", "fast"):
        try:
            cp = llm_analyze.cache_path(slots, llm_analyze.load_config(), mode)
            if os.path.isfile(cp):
                with open(cp, encoding="utf-8") as fh:
                    res[mode] = json.load(fh)
        except Exception:
            pass
    return res


def library_detail(bid, fname):
    b = read_batch(bid)
    if not b:
        return None
    d = batch_dir(bid)
    fn = os.path.basename(fname)
    png = os.path.join(d, "images", fn)
    if not os.path.isfile(png):
        return None
    stem = os.path.splitext(fn)[0]

    with open(png, "rb") as f:
        blob = f.read()
    try:
        orig, how = repro.extract_params(blob, fn)
    except Exception as e:
        orig, how = {}, f"读取失败: {e}"

    c = orig.get("Comment", orig) if isinstance(orig, dict) else orig
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except Exception:
            c = {}

    def jload(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    req = os.path.join(d, "requests", stem + ".json")
    rep = os.path.join(d, "meta", stem + ".report.json")
    org = os.path.join(d, "meta", f"{_work_of_png(fn)}.original.json")
    pr = prompts_of(orig)

    return {
        "batch": bid, "batch_name": b.get("name") or bid, "f": fn, "png": png,
        "meta_from": how,
        "source": repro.image_source(blob, orig),
        "params": {k: c.get(k) for k in (
            "model_name", "model_hash", "seed", "steps", "scale", "sampler",
            "noise_schedule", "cfg_rescale", "width", "height", "sm", "sm_dyn",
            "prefer_brownian", "deliberate_euler_ancestral_bug",
            "quality_boost", "straight_alpha", "request_type", "signed_hash")},
        "prompts": pr,
        "paths": {"png": png,
                  "request": req if os.path.isfile(req) else None,
                  "report": rep if os.path.isfile(rep) else None,
                  "original": org if os.path.isfile(org) else None},
        "report": jload(rep),
        "request": jload(req),
        "llm": _llm_cache_for(pr),
        "conditioned": repro.conditioning_usage(c),
    }


# ---------------------------------------------------------------- 批次整理

def batch_work(bid):
    b = read_batch(bid) or {}
    names = []
    for it in (b.get("items") or []):
        if it.get("name"):
            names.append(re.sub(r"_p\d+$", "", str(it["name"]).split(".")[0]))
    if not names:
        d = os.path.join(batch_dir(bid), "images")
        if os.path.isdir(d):
            names = [_work_of_png(f) for f in os.listdir(d)
                     if f.lower().endswith(".png")]
    if not names:
        return None
    return max(set(names), key=names.count)


def autoname_batches(only_ugly=True):
    changed = []
    for b in list_batches():
        bid = b["id"]
        cur = str(b.get("name") or "")
        if only_ugly and cur and not re.match(r"^\d{8}-\d{6}", cur):
            continue          # 手改过的不动
        d = batch_dir(bid)
        imgdir = os.path.join(d, "images")
        if not os.path.isdir(imgdir):
            continue
        files = [f for f in os.listdir(imgdir) if f.lower().endswith(".png")]
        if not files:
            continue
        work = batch_work(bid) or _work_of_png(files[0])
        try:
            ts = time.strftime("%m-%d %H:%M", time.localtime(
                min(os.path.getmtime(os.path.join(imgdir, f)) for f in files)))
        except Exception:
            ts = (b.get("created") or "")[5:16].replace("T", " ")
        new = f"{work} · {ts}".strip(" ·")
        if new and new != cur:
            b["name"] = new
            write_batch(bid, b)
            changed.append({"id": bid, "from": cur, "to": new})
    return changed


def merge_batches(ids, name=None):
    ids = [i for i in ids if read_batch(i)]
    if len(ids) < 2:
        return None

    def imgcount(bid):
        d = os.path.join(batch_dir(bid), "images")
        return len([f for f in os.listdir(d) if f.lower().endswith(".png")]) \
            if os.path.isdir(d) else 0

    ids.sort(key=lambda x: -imgcount(x))
    target, tb = ids[0], read_batch(ids[0])
    tdir = batch_dir(target)
    items = list(tb.get("items") or [])
    moved = 0

    for src in ids[1:]:
        sb = read_batch(src) or {}
        sdir = batch_dir(src)
        s_items = {str(i.get("png", "")).split("/")[-1]: i
                   for i in (sb.get("items") or [])}
        for sub in ("images", "requests", "meta"):
            sd = os.path.join(sdir, sub)
            if not os.path.isdir(sd):
                continue
            for fn in sorted(os.listdir(sd)):
                sp = os.path.join(sd, fn)
                if not os.path.isfile(sp):
                    continue
                os.makedirs(os.path.join(tdir, sub), exist_ok=True)
                dp = repro.unique_path(os.path.join(tdir, sub, fn))
                shutil.move(sp, dp)
                moved += 1
                if sub != "images":
                    continue
                newfn = os.path.basename(dp)
                st = os.path.splitext(newfn)[0]
                it = dict(s_items.get(fn) or {})
                it.setdefault("name", _work_of_png(newfn))
                it["png"] = f"images/{newfn}"
                if os.path.isfile(os.path.join(tdir, "requests", st + ".json")):
                    it["request"] = f"requests/{st}.json"
                if os.path.isfile(os.path.join(tdir, "meta", st + ".report.json")):
                    it["report"] = f"meta/{st}.report.json"
                items.append(it)
        shutil.rmtree(sdir, ignore_errors=True)

    tb["items"] = items
    if name:
        tb["name"] = name
    write_batch(target, tb)
    return {"target": target, "name": tb.get("name"),
            "merged": len(ids) - 1, "moved": moved}


def autogroup_batches():
    groups = {}
    for b in list_batches():
        w = batch_work(b["id"])
        if w:
            groups.setdefault(w, []).append(b["id"])
    results = []
    for w, ids in groups.items():
        if len(ids) < 2:
            continue
        r = merge_batches(ids)
        if r:
            r["work"] = w
            results.append(r)
    return results


# ---------------------------------------------------------------- 缩略图

THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)


def thumb(src, tag, width):
    dst = os.path.join(THUMB_DIR, f"{tag}_{width}.jpg")
    if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    try:
        im = Image.open(src)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))),
                           Image.LANCZOS)
        im.save(dst, "JPEG", quality=84)
    except Exception:
        return None
    return dst


def batch_thumb(bid, fname, width=380):
    src = os.path.join(batch_dir(bid), "images", os.path.basename(fname))
    if not os.path.isfile(src):
        return None
    tag = hashlib.sha1(f"{bid}/{fname}".encode()).hexdigest()[:16]
    return thumb(src, tag, width)


# ---------------------------------------------------------------- HTTP

CTYPE = {".html": "text/html; charset=utf-8", ".png": "image/png",
         ".jpg": "image/jpeg", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "NAILibrary/1.0"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def send_file(self, path, ctype=None):
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or CTYPE.get(
            os.path.splitext(path)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _guard(self, p):
        """只允许访问 NAI 的 out 目录与本项目 cache。"""
        ap = os.path.abspath(p)
        for root in (OUT, CACHE_DIR):
            try:
                if os.path.commonpath([ap, root]) == os.path.abspath(root):
                    return ap
            except ValueError:
                pass
        return None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            self.send_file(os.path.join(HERE, "lib.html"))
        elif u.path == "/api/library":
            bid = q.get("batch", [""])[0] or None
            items = library_index(bid, q.get("sort", ["time"])[0])
            self.send_json({"ok": True, "items": items,
                            "batches": [{"id": b["id"], "name": b.get("name"),
                                         "count": b.get("image_count")}
                                        for b in list_batches()]})
        elif u.path == "/api/library/item":
            d = library_detail(q.get("batch", [""])[0], q.get("f", [""])[0])
            self.send_json({"ok": bool(d), "item": d,
                            "error": None if d else "找不到这张图"})
        elif u.path == "/api/batches":
            self.send_json({"ok": True, "batches": list_batches()})
        elif u.path == "/api/status":
            self.send_json({"ok": True, "nai_dir": NAI_DIR,
                            "batches": len(list_batches()),
                            "images": len(library_index())})
        elif u.path == "/img":
            kind = q.get("kind", ["thumb"])[0]
            bid = q.get("id", [""])[0]
            f = os.path.basename(q.get("f", [""])[0])
            try:
                w = max(64, min(1600, int(q.get("w", ["380"])[0])))
            except (ValueError, TypeError):
                w = 380
            p = batch_thumb(bid, f, w) if kind == "thumb" else \
                os.path.join(batch_dir(bid), "images", f)
            p = self._guard(p) if p else None
            if not p:
                self.send_error(404)
            else:
                self.send_file(p)
        else:
            self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            data = self.body()
        except Exception as e:
            self.send_json({"ok": False, "error": f"bad body: {e}"}, 400)
            return

        if u.path == "/api/batches/rename":
            bid = data.get("id")
            b = read_batch(bid)
            if not b:
                self.send_json({"ok": False, "error": "批次不存在"}, 400)
                return
            b["name"] = (data.get("name") or "").strip() or b["name"]
            write_batch(bid, b)
            self.send_json({"ok": True, "name": b["name"]})

        elif u.path == "/api/batches/autoname":
            ch = autoname_batches(only_ugly=not data.get("all"))
            self.send_json({"ok": True, "changed": ch})

        elif u.path == "/api/batches/merge":
            if data.get("auto"):
                self.send_json({"ok": True, "groups": autogroup_batches()})
                return
            r = merge_batches([i for i in (data.get("ids") or []) if i],
                              data.get("name"))
            self.send_json({"ok": bool(r), "result": r,
                            "error": None if r else "至少选两个批次"})

        elif u.path == "/api/batches/delete":
            bid = data.get("id")
            if not read_batch(bid) or not data.get("confirm"):
                self.send_json({"ok": False, "error": "批次不存在或未确认"}, 400)
                return
            d = batch_dir(bid)
            if os.path.commonpath([os.path.abspath(d),
                                   os.path.abspath(BATCH_ROOT)]) \
                    != os.path.abspath(BATCH_ROOT):
                self.send_json({"ok": False, "error": "路径非法"}, 400)
                return
            shutil.rmtree(d, ignore_errors=True)
            self.send_json({"ok": True})

        elif u.path == "/api/reveal":
            p = self._guard(data.get("path") or "")
            if not p or not os.path.exists(p):
                self.send_json({"ok": False, "error": "路径不存在或越界"}, 400)
                return
            try:
                if os.path.isfile(p):
                    os.system(f'explorer /select,"{p}"')
                else:
                    os.startfile(p)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif u.path == "/api/export":
            # 把选中的图复制到一个扁平目录，方便直接拿出去用
            items = data.get("items") or []
            dest = (data.get("dest") or "").strip()
            if not items or not dest:
                self.send_json({"ok": False, "error": "缺 items 或 dest"}, 400)
                return
            try:
                os.makedirs(dest, exist_ok=True)
                n = 0
                for it in items:
                    src = os.path.join(batch_dir(it.get("batch", "")),
                                       "images", os.path.basename(it.get("f", "")))
                    if os.path.isfile(src):
                        shutil.copy2(src, repro.unique_path(
                            os.path.join(dest, os.path.basename(src))))
                        n += 1
                self.send_json({"ok": True, "copied": n, "dest": dest})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description="NAI 资源库")
    ap.add_argument("--port", type=int, default=CFG["port"])
    ap.add_argument("--host", default=CFG["host"])
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    if not os.path.isdir(BATCH_ROOT):
        print(f"[warn] 找不到批次目录 {BATCH_ROOT}")
        print(f"       请检查 lib_config.json 里的 nai_dir 是否正确")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://{a.host}:{a.port}/"
    print(f"NAI 资源库  ->  {url}")
    print(f"数据来源: {OUT}")
    if a.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
