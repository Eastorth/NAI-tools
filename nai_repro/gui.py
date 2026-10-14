#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAI 复现工具 —— 本地 GUI 服务端

只依赖 Python 标准库 + Pillow。启动后浏览器访问 http://127.0.0.1:<port>/

    python gui.py            # 默认 8770
    python gui.py --open     # 顺便自动打开浏览器
"""

import argparse
import copy
import hashlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402
import llm_analyze  # noqa: E402
import repro  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
SRC_DIR = os.path.join(OUT, "_src")
THUMB_DIR = os.path.join(OUT, "_thumbs")
BATCH_ROOT = os.path.join(OUT, "batches")
for d in (OUT, SRC_DIR, THUMB_DIR, BATCH_ROOT):
    os.makedirs(d, exist_ok=True)

DEFAULT_SETTINGS = {
    "model": "nai-diffusion-5-full",
    "sampler": "k_euler_ancestral",
    "width": 1280,
    "height": 768,
    "keep_size": False,
    "steps": 23,
    "scale": 7.0,
    "seed": "",
    "random_seed": False,
    "noise_schedule": "",
    "prompt": "",
    "drop_characters": False,
    "keep_image_refs": False,
    "llm_mode": "full",
    "proxy": repro.DEFAULT_PROXY,
    "referer": repro.DEFAULT_REFERER,
    "sleep": 3.0,
}

MODELS = [
    "nai-diffusion-5-full",
    "nai-diffusion-5-curated",
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
    "nai-diffusion-4-full",
    "nai-diffusion-3",
]

SAMPLERS = [
    "k_euler_ancestral", "k_euler", "k_dpmpp_2m", "k_dpmpp_2s_ancestral",
    "k_dpmpp_sde", "k_dpmpp_2m_sde", "ddim", "k_dpm_2", "k_dpm_2_ancestral",
    "k_heun", "k_lms", "plms", "ddim_v3",
]


# ---------------------------------------------------------------- 状态

def read_token():
    """Token 来源：环境变量 NAI_TOKEN，其次项目目录下的 token.txt。"""
    t = (os.environ.get("NAI_TOKEN") or "").strip()
    if t:
        return t
    for name in ("token.txt", ".nai_token"):
        p = os.path.join(HERE, name)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    t = f.read().strip()
                if t:
                    return t
            except Exception:
                pass
    return ""


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}          # key -> UI 用摘要
        self.raw = {}            # key -> 完整原始元数据
        self.orig_prompts = {}   # key -> 未经编辑的提示词，用于「还原」
        self.order = []
        self.log = []
        self.phase = "空闲"
        self.running = False
        self.cancel = False
        self.done = 0
        self.total = 0
        self.rev = 0
        self.prompt_rev = 0        # 提示词内容的版本号，变了就重算共有词
        self.batch_mode = "auto"   # "auto" = 当天每日批次（凌晨4点换天）；否则是批次 id
        self.last_batch = None     # 最近一次生成写入的批次
        self.llm = {"running": False, "done": 0, "total": 0,
                    "current": None, "phase": "空闲"}
        self.llm_cfg = llm_analyze.load_config()
        self.settings = dict(DEFAULT_SETTINGS)
        self.token = read_token()

    def bump(self):
        with self.lock:
            self.rev += 1

    def say(self, msg):
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.log.append(f"[{stamp}] {msg}")
            self.log = self.log[-400:]
            self.rev += 1

    def snapshot(self):
        with self.lock:
            return {
                "phase": self.phase,
                "running": self.running,
                "done": self.done,
                "total": self.total,
                "rev": self.rev,
                "prompt_rev": self.prompt_rev,
                "log": list(self.log),
                "order": list(self.order),
                "items": {k: self.items[k] for k in self.order},
                "settings": dict(self.settings),
                "has_token": bool(self.token),
                "models": MODELS,
                "samplers": SAMPLERS,
                "cats": analyze.ORDER + [analyze.UNKNOWN],
                "out_dir": OUT,
                "batch_root": BATCH_ROOT,
                "batch_mode": self.batch_mode,
                "last_batch": self.last_batch,
                "llm": dict(self.llm),
                "llm_cfg": {k: v for k, v in self.llm_cfg.items()
                            if k in ("backend", "base_url", "model")},
                "llm_cats": llm_analyze.CATEGORIES,
            }


STATE = State()


# ---------------------------------------------------------------- 工具

def key_of(src):
    return hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]


def save_thumb(blob, tag, width=420):
    im = Image.open(io.BytesIO(blob))
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    if im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
    p = os.path.join(THUMB_DIR, f"{tag}.jpg")
    im.save(p, "JPEG", quality=84)
    return p


def make_args(st, token, proxy):
    s = st.settings
    seed = str(s.get("seed", "")).strip()
    return SimpleNamespace(
        model=s["model"],
        sampler=s["sampler"],
        size=(int(s["width"]), int(s["height"])),
        keep_size=bool(s.get("keep_size")),
        steps=int(s["steps"]),
        scale=float(s["scale"]),
        seed=int(seed) if seed else None,
        random_seed=bool(s.get("random_seed")),
        noise_schedule=(s.get("noise_schedule") or None),
        prompt=(s.get("prompt") or None),
        drop_characters=bool(s.get("drop_characters")),
        keep_image_refs=bool(s.get("keep_image_refs")),
        proxy=proxy,
        token=token,
        out=OUT,
        dry_run=False,
        compare_hash=False,
    )


def summarize(orig, name, src, thumb_ok):
    c = orig.get("Comment", orig) if isinstance(orig, dict) else orig
    if isinstance(c, str):
        c = json.loads(c)
    cap = (c.get("v4_prompt", {}) or {}).get("caption", {}) or {}
    return {
        "name": name,
        "src": src,
        "status": "已解析",
        "error": None,
        "model_source": (orig.get("Source") if isinstance(orig, dict) else None),
        "width": c.get("width"),
        "height": c.get("height"),
        "seed": c.get("seed"),
        "steps": c.get("steps"),
        "scale": c.get("scale"),
        "sampler": c.get("sampler"),
        "noise_schedule": c.get("noise_schedule"),
        "cfg_rescale": c.get("cfg_rescale"),
        "nchar": len(cap.get("char_captions") or []),
        "prompt_head": (cap.get("base_caption") or c.get("prompt") or "")[:90],
        "thumb": thumb_ok,
        "conditioned": repro.conditioning_usage(c),
    }


def comment_of(orig):
    c = orig.get("Comment", orig) if isinstance(orig, dict) else orig
    if isinstance(c, str):
        c = json.loads(c)
    return c


def prompts_of(orig):
    """抽出四个提示词区块。角色提示词保留完整对象（含 centers），只改 char_caption。"""
    c = comment_of(orig)
    vp = (c.get("v4_prompt") or {}).get("caption") or {}
    vn = (c.get("v4_negative_prompt") or {}).get("caption") or {}

    def chars(seq):
        out = []
        for it in (seq or []):
            if isinstance(it, dict):
                out.append({"char_caption": it.get("char_caption", ""),
                            "centers": it.get("centers")})
            else:
                out.append({"char_caption": str(it), "centers": None})
        return out

    return {
        "pos_base": vp.get("base_caption") or c.get("prompt") or "",
        "pos_chars": chars(vp.get("char_captions")),
        "neg_base": vn.get("base_caption") or c.get("uc") or "",
        "neg_chars": chars(vn.get("char_captions")),
    }


def apply_prompts(payload, pr, drop_chars=False):
    """把编辑后的提示词写回请求体。"""
    p = payload["parameters"]
    vp = p.setdefault("v4_prompt", {}).setdefault("caption", {})
    vn = p.setdefault("v4_negative_prompt", {}).setdefault("caption", {})

    vp["base_caption"] = pr["pos_base"]
    vn["base_caption"] = pr["neg_base"]
    vp["char_captions"] = [] if drop_chars else [copy.deepcopy(c) for c in pr["pos_chars"]]
    vn["char_captions"] = [] if drop_chars else [copy.deepcopy(c) for c in pr["neg_chars"]]

    p["prompt"] = pr["pos_base"]
    p["uc"] = pr["neg_base"]
    payload["input"] = pr["pos_base"]
    return payload


TARGETS = {
    "pos_base": ("pos_base", None),
    "pos_chars": ("pos_chars", "char_caption"),
    "neg_base": ("neg_base", None),
    "neg_chars": ("neg_chars", "char_caption"),
}


def do_replace(pr, find, repl, use_regex, ignore_case, targets, cats_filter=None):
    """对单个提示词集合做替换，返回 (新集合, 命中次数)。pr 会被就地修改。

    cats_filter 非空时，只在指定语义分类的标签内替换。
    """
    if cats_filter:
        hits = 0
        for t in targets:
            field, sub = TARGETS[t]
            if field not in pr:
                continue
            if sub is None:
                pr[field], n = analyze.replace_in_categories(
                    pr[field], find, repl, cats_filter, use_regex, ignore_case)
                hits += n
            else:
                for item in pr[field]:
                    new, n = analyze.replace_in_categories(
                        item.get(sub) or "", find, repl, cats_filter,
                        use_regex, ignore_case)
                    if n:
                        item[sub] = new
                        hits += n
        return pr, hits

    flags = re.IGNORECASE if ignore_case else 0
    pat = re.compile(find if use_regex else re.escape(find), flags)
    hits = 0

    for t in targets:
        field, sub = TARGETS[t]
        if field not in pr:
            continue
        if sub is None:
            new, n = pat.subn(repl, pr[field])
            if n:
                pr[field] = new
                hits += n
        else:
            for item in pr[field]:
                new, n = pat.subn(repl, item.get(sub) or "")
                if n:
                    item[sub] = new
                    hits += n
    return pr, hits


def count_hits(pr, find, use_regex, ignore_case, targets, cats_filter=None):
    if cats_filter:
        n = 0
        for t in targets:
            field, sub = TARGETS[t]
            if field not in pr:
                continue
            if sub is None:
                _, k = analyze.replace_in_categories(
                    pr[field], find, "", cats_filter, use_regex, ignore_case)
                n += k
            else:
                for item in pr[field]:
                    _, k = analyze.replace_in_categories(
                        item.get(sub) or "", find, "", cats_filter,
                        use_regex, ignore_case)
                    n += k
        return n

    flags = re.IGNORECASE if ignore_case else 0
    pat = re.compile(find if use_regex else re.escape(find), flags)
    n = 0
    for t in targets:
        field, sub = TARGETS[t]
        if field not in pr:
            continue
        if sub is None:
            n += len(pat.findall(pr[field]))
        else:
            for item in pr[field]:
                n += len(pat.findall(item.get(sub) or ""))
    return n


# ---------------------------------------------------------------- 跨图共有词

def item_blocks(pr):
    """提示词块的规范 id —— 必须和前端 renderPrompt 用的 id 一致。"""
    out = [("pos_base", pr.get("pos_base") or ""),
           ("neg_base", pr.get("neg_base") or "")]
    for i, c in enumerate(pr.get("pos_chars") or []):
        out.append((f"pos_chars_{i}", c.get("char_caption") or ""))
    for i, c in enumerate(pr.get("neg_chars") or []):
        out.append((f"neg_chars_{i}", c.get("char_caption") or ""))
    return out


def share_level(count, total):
    if total <= 1:
        return "all" if count >= 1 else "one"
    if count >= total:
        return "all"
    if count >= max(2, round(total * 0.6)):
        return "most"
    if count >= 2:
        return "some"
    return "one"


def build_spans(text, freq, total, cats):
    """切成 [标签 | 间隙] 片段，逐字节可还原原文。"""
    tags = [t for t in analyze.parse_tags(text) if t.get("rstart") is not None]
    tags.sort(key=lambda t: t["rstart"])
    out, pos = [], 0
    for tg in tags:
        a, b = tg["rstart"], tg["rend"]
        if a < pos or b <= a:
            continue
        if a > pos:
            out.append({"t": text[pos:a], "k": "gap"})
        n = tg["text"].strip().lower()
        c = freq.get(n, 1)
        out.append({"t": text[a:b], "k": share_level(c, total), "c": c,
                    "cat": analyze.classify(tg["text"], cats), "w": tg["weight"]})
        pos = b
    if pos < len(text):
        out.append({"t": text[pos:], "k": "gap"})
    return out


def compute_common(keys):
    cats = analyze.load_categories()
    freq, per = {}, {}
    for k in keys:
        pr = (STATE.items.get(k) or {}).get("prompts")
        if not isinstance(pr, dict):
            continue
        blocks = item_blocks(pr)
        per[k] = blocks
        seen = set()
        for _bid, txt in blocks:
            for tg in analyze.parse_tags(txt):
                n = tg["text"].strip().lower()
                if n and n not in seen:
                    seen.add(n)
                    freq[n] = freq.get(n, 0) + 1

    total = len(per)
    spans = {k: {bid: build_spans(txt, freq, total, cats) for bid, txt in blocks}
             for k, blocks in per.items()}

    groups = {"all": [], "most": [], "some": [], "one": []}
    for n, c in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])):
        groups[share_level(c, total)].append({"text": n, "count": c})
    return {"total": total, "freq": freq, "spans": spans, "groups": groups}


_COMMON_CACHE = {"rev": None, "keys": None, "data": None}


# ---------------------------------------------------------------- 批次

def _slug(s, maxlen=32):
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", (s or "").strip()).strip("-.")
    return s[:maxlen]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def batch_dir(bid):
    return os.path.join(BATCH_ROOT, bid)


def read_batch(bid):
    if not bid or os.sep in bid or "/" in bid or ".." in bid:
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
    data["updated"] = _now()
    with open(os.path.join(d, "batch.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def create_batch(name=None, settings=None, bid=None):
    """新建批次。bid 为空时按时间戳自动生成（手动新建的独立批次走这条）。"""
    st = dict(settings or STATE.settings)
    if not bid:
        slug = _slug(name) or _slug(st.get("model", "").replace("nai-diffusion-", "nai"))
        bid = time.strftime("%Y%m%d-%H%M%S") + (f"_{slug}" if slug else "")
        n = 2
        while os.path.exists(batch_dir(bid)):
            bid = time.strftime("%Y%m%d-%H%M%S") + (f"_{slug}" if slug else "") + f"-{n}"
            n += 1
    data = write_batch(bid, {
        "name": name or bid,
        "created": _now(),
        "settings": {k: v for k, v in st.items() if k != "token"},
        "items": [],
    })
    return bid, data


# ---------------------------------------------------------------- 每日批次

DAY_START_HOUR = 4        # 凌晨 4 点换天：4 点前的生成算前一天


def day_anchor(t=None):
    """把时间往前推 DAY_START_HOUR 小时，这样"一天"就从凌晨 4 点开始。"""
    return (time.time() if t is None else t) - DAY_START_HOUR * 3600


def day_key(t=None):
    return time.strftime("%Y%m%d", time.localtime(day_anchor(t)))


def day_label(t=None):
    lt = time.localtime(day_anchor(t))
    return time.strftime("%Y-%m-%d", lt)


def day_window(t=None):
    """当天的起止时间戳，用于界面展示。"""
    a = day_anchor(t)
    lt = time.localtime(a)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                         DAY_START_HOUR, 0, 0, 0, 0, -1))
    return start, start + 86400


def ensure_day_batch(settings=None):
    """取「今天」的批次；还没有就建一个。一天只有一个。"""
    bid = "day_" + day_key()
    b = read_batch(bid)
    if b:
        return bid, b
    return create_batch(f"{day_label()} 每日批次", settings, bid=bid)


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
                                if x.lower().endswith(".png")]) if os.path.isdir(imgdir) else 0
        b["item_count"] = len(b.get("items") or [])
        out.append(b)
    return out


def add_batch_item(bid, entry):
    if not bid:
        return
    b = read_batch(bid)
    if not b:
        return
    b.setdefault("items", []).append(entry)
    write_batch(bid, b)


def batch_gallery(bid):
    b = read_batch(bid)
    if not b:
        return None
    d = os.path.join(batch_dir(bid), "images")
    files = []
    if os.path.isdir(d):
        files = sorted((x for x in os.listdir(d) if x.lower().endswith(".png")), reverse=True)
    return {"batch": b, "files": files}


def batch_thumb(bid, fname, width=380):
    """批次图片的缩略图，按需生成并缓存到 _thumbs。"""
    src = os.path.join(batch_dir(bid), "images", os.path.basename(fname))
    if not os.path.isfile(src):
        return None
    tag = "b_" + hashlib.sha1(f"{bid}/{fname}/{width}".encode()).hexdigest()[:16]
    dst = os.path.join(THUMB_DIR, tag + ".jpg")
    if not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        try:
            with open(src, "rb") as f:
                save_thumb(f.read(), tag, width=width)
        except Exception:
            return None
    return dst if os.path.isfile(dst) else None


def compute_common_cached(keys):
    keys = list(keys)
    if (_COMMON_CACHE["data"] is not None
            and _COMMON_CACHE["rev"] == STATE.prompt_rev
            and _COMMON_CACHE["keys"] == keys):
        return _COMMON_CACHE["data"]
    d = compute_common(keys)
    _COMMON_CACHE.update({"rev": STATE.prompt_rev, "keys": keys, "data": d})
    return d


# ---------------------------------------------------------------- 解析任务

def job_parse(sources):
    with STATE.lock:
        STATE.running = True
        STATE.cancel = False
        STATE.phase = "解析参数"
        STATE.done = 0
        STATE.total = len(sources)
        STATE.rev += 1

    proxy = STATE.settings.get("proxy") or None
    referer = STATE.settings.get("referer") or None

    for i, src in enumerate(sources):
        if STATE.cancel:
            STATE.say("已取消")
            break
        k = key_of(src)
        name = os.path.basename(src.split("?")[0]) or src
        try:
            STATE.say(f"取数 {name} …")
            orig, how, name, blob = repro.load_source(src, proxy, referer)

            with open(os.path.join(SRC_DIR, k + ".bin"), "wb") as f:
                f.write(blob)
            # 记下来源 URL，便于排查 / 重新解析
            try:
                with open(os.path.join(SRC_DIR, k + ".url.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(src)
            except Exception:
                pass
            thumb = None
            try:
                thumb = f"/img?kind=thumb&key={urllib.parse.quote(k)}"
                save_thumb(blob, k)
            except Exception as e:
                STATE.say(f"  缩略图失败: {e}")

            item = summarize(orig, name, src, thumb)
            item["id"] = k
            item["how"] = how
            pr = prompts_of(orig)
            item["prompts"] = pr
            item["edited"] = False
            with STATE.lock:
                STATE.raw[k] = orig
                STATE.orig_prompts[k] = copy.deepcopy(pr)
                if k not in STATE.items:
                    STATE.order.append(k)
                STATE.items[k] = item
        except BaseException as e:
            STATE.say(f"  !! {name} 失败: {e}")
            with STATE.lock:
                if k not in STATE.items:
                    STATE.order.append(k)
                STATE.items[k] = {"id": k, "name": name, "src": src,
                                  "status": "失败", "error": str(e)}
        with STATE.lock:
            STATE.done = i + 1
            STATE.rev += 1

    with STATE.lock:
        STATE.running = False
        STATE.phase = "空闲"
        STATE.prompt_rev += 1
        STATE.rev += 1
    STATE.say("解析完成")


# ---------------------------------------------------------------- 生成任务

def job_run(keys):
    with STATE.lock:
        STATE.running = True
        STATE.cancel = False
        STATE.phase = "生成中"
        STATE.done = 0
        STATE.total = len(keys)
        STATE.rev += 1

    token = STATE.token
    proxy = STATE.settings.get("proxy") or None
    sleep = float(STATE.settings.get("sleep", 3.0))
    args = make_args(STATE, token, proxy)

    # 决定这批结果写进哪个批次
    # auto = 当天的每日批次（凌晨 4 点换天）；否则是指定的批次 id
    mode = STATE.batch_mode
    if mode and mode != "auto":
        b = read_batch(mode)
        bid = mode if b else None
    else:
        b, bid = None, None
    if not b:
        bid, b = ensure_day_batch(STATE.settings)
    STATE.last_batch = bid
    bdir = batch_dir(bid)
    dirs = repro.out_dirs(bdir)
    STATE.say(f"批次「{b['name']}」({bid})")

    for i, k in enumerate(keys):
        if STATE.cancel:
            STATE.say("已取消")
            break
        with STATE.lock:
            item = STATE.items.get(k, {})
            orig = STATE.raw.get(k)
        name = item.get("name", k)
        if orig is None:
            STATE.say(f"跳过 {name}：没有原始参数")
            continue
        payload = None
        try:
            with STATE.lock:
                item["status"] = "构建请求"
                item["error"] = None
                STATE.rev += 1

            payload, trace = repro.build_v5_payload(orig, args)

            # 把（可能已编辑的）提示词写回请求体
            pr = item.get("prompts")
            base_pr = STATE.orig_prompts.get(k)
            if pr:
                apply_prompts(payload, pr,
                              drop_chars=bool(STATE.settings.get("drop_characters")))
                if base_pr and base_pr != pr:
                    for lbl, f in (("基础提示词", "pos_base"), ("负面基础", "neg_base")):
                        if base_pr.get(f) != pr.get(f):
                            trace.append((lbl, str(base_pr.get(f))[:70],
                                          str(pr.get(f))[:70], "已编辑"))
                    for lbl, f in (("正向角色词", "pos_chars"), ("负向角色词", "neg_chars")):
                        a = [x["char_caption"] for x in base_pr.get(f, [])]
                        b = [x["char_caption"] for x in pr.get(f, [])]
                        if a != b:
                            trace.append((lbl, f"{len(a)} 条（原）", f"{len(b)} 条（编辑后）", "已编辑"))
            p = payload["parameters"]

            with STATE.lock:
                item["sent"] = {kk: p.get(kk) for kk in (
                    "sampler", "steps", "scale", "width", "height", "seed",
                    "noise_schedule", "cfg_rescale", "prefer_brownian",
                    "deliberate_euler_ancestral_bug", "sm", "sm_dyn")}
                item["overrides"] = [{"field": a, "from": str(b)[:160],
                                      "to": str(cc)[:160], "note": d}
                                     for a, b, cc, d in trace]
                item["status"] = "生成中"
                STATE.rev += 1

            STATE.say(f"生成 {name} …")
            t0 = time.time()
            raw, ctype = repro.submit(payload, token, proxy, trace=trace)
            png = repro.unpack_png(raw)
            dt = time.time() - t0

            # submit 可能因服务端校验而自愈（置空某些字段），覆盖记录要重新取
            with STATE.lock:
                item["overrides"] = [{"field": a, "from": str(b)[:160],
                                      "to": str(cc)[:160], "note": d}
                                     for a, b, cc, d in trace]

            stem = os.path.splitext(item.get("name", k))[0]
            seed_used = payload["parameters"].get("seed")
            base = f"{stem}_{seed_used}"

            png_path = repro.unique_path(os.path.join(dirs["images"], base + ".png"))
            with open(png_path, "wb") as f:
                f.write(png)
            req_path = repro.unique_path(os.path.join(dirs["requests"], base + ".json"))
            with open(req_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            orig_path = os.path.join(dirs["meta"], f"{stem}.original.json")
            with open(orig_path, "w", encoding="utf-8") as f:
                json.dump(orig, f, ensure_ascii=False, indent=2)

            def rel(p):
                return os.path.relpath(p, bdir).replace("\\", "/")

            got, how = repro.extract_params(png, stem)
            orig_c = orig.get("Comment", orig) if isinstance(orig, dict) else orig
            if isinstance(orig_c, str):
                orig_c = json.loads(orig_c)
            rows, prompt_rows = repro.compare(orig_c, p, got)

            try:
                save_thumb(png, k + "_out", width=520)
                out_thumb = f"/img?kind=thumb&key={urllib.parse.quote(k + '_out')}"
            except Exception:
                out_thumb = None

            with STATE.lock:
                item["status"] = "完成"
                item["seconds"] = round(dt, 1)
                item["seed_used"] = seed_used
                item["request_json"] = req_path
                item["out_png"] = png_path
                item["out_thumb"] = out_thumb
                item["out_img"] = f"/img?kind=out&key={urllib.parse.quote(k)}"
                item["returned"] = {
                    "model_name": got.get("model_name"),
                    "model_hash": got.get("model_hash"),
                    "source": Image.open(io.BytesIO(png)).info.get("Source"),
                    "signed_hash": (got.get("signed_hash") or "")[:28],
                    "request_type": got.get("request_type"),
                }
                item["checks"] = [
                    {"field": a, "original": str(b)[:60], "sent": str(cc)[:60],
                     "returned": str(dd)[:60], "srv_ok": cc == dd, "same_orig": b == cc}
                    for a, b, cc, dd in rows
                ]
                item["prompt_checks"] = [
                    {"field": a, "original": b, "returned": cc, "verdict": dd}
                    for a, b, cc, dd in prompt_rows
                ]
                STATE.rev += 1
            STATE.say(f"  {name} 完成，{dt:.1f}s -> {png_path}")

            # 报告落盘 + 记入批次清单
            rep = {
                "source": item.get("src"),
                "name": item.get("name"),
                "model": args.model,
                "seed_used": seed_used,
                "seconds": round(dt, 1),
                "original_source": (orig.get("Source") if isinstance(orig, dict) else None),
                "returned": item["returned"],
                "overrides": item.get("overrides") or [],
                "check": item["checks"],
                "prompt_check": item["prompt_checks"],
                "output_png": rel(png_path),
            }
            rep_path = os.path.join(dirs["meta"], f"{stem}_{seed_used}.report.json")
            with open(rep_path, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=2)

            add_batch_item(bid, {
                "key": k,
                "name": item.get("name"),
                "src": item.get("src"),
                "seed": seed_used,
                "status": "完成",
                "seconds": round(dt, 1),
                "model": args.model,
                "model_hash": (item.get("returned") or {}).get("model_hash"),
                "signed_hash": (item.get("returned") or {}).get("signed_hash"),
                "png": rel(png_path),
                "request": rel(req_path),
                "original": rel(orig_path),
                "report": rel(rep_path),
                "width": p.get("width"),
                "height": p.get("height"),
                "steps": p.get("steps"),
                "scale": p.get("scale"),
                "sampler": p.get("sampler"),
                "finished": _now(),
            })

        except BaseException as e:
            STATE.say(f"  !! {name} 失败: {e}")
            with STATE.lock:
                item["status"] = "失败"
                item["error"] = str(e)
                STATE.rev += 1
            # 失败也把请求体落盘，便于排查
            try:
                if payload is not None:
                    stem = os.path.splitext(item.get("name", k))[0]
                    failed = os.path.join(
                        dirs["requests"],
                        f"{stem}_{payload['parameters'].get('seed')}.FAILED.json")
                    with open(failed, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
                    STATE.say(f"     失败请求已存盘: {os.path.basename(failed)}")
            except Exception:
                pass

        with STATE.lock:
            STATE.done = i + 1
            STATE.rev += 1
        if i < len(keys) - 1 and not STATE.cancel:
            time.sleep(sleep)

    with STATE.lock:
        STATE.running = False
        STATE.phase = "空闲"
        STATE.rev += 1
    STATE.say("任务结束")


# ---------------------------------------------------------------- LLM 分析任务

def job_llm(keys, force=False, mode=None):
    cfg = dict(STATE.llm_cfg)
    mode = mode or STATE.settings.get("llm_mode") or "full"
    if mode not in llm_analyze.MODES:
        mode = "full"
    with STATE.lock:
        STATE.llm.update({"running": True, "done": 0, "total": len(keys),
                          "current": None, "phase": "分析中", "mode": mode})
        STATE.rev += 1

    ok, msg = llm_analyze.check(cfg)
    STATE.say(f"LLM：{msg}")
    if not ok:
        with STATE.lock:
            STATE.llm.update({"running": False, "phase": "空闲"})
            STATE.rev += 1
        return

    for i, k in enumerate(keys):
        with STATE.lock:
            item = STATE.items.get(k)
            prompts = (item or {}).get("prompts")
            if item is not None:
                STATE.llm["current"] = item.get("name")
        if not prompts:
            STATE.say(f"跳过 {k}：没有提示词")
        else:
            STATE.say(f"分析 {item.get('name')} …")
            with STATE.lock:
                item["llm"] = {"status": "分析中"}
                STATE.rev += 1
            try:
                r = llm_analyze.analyze_prompts(prompts, cfg, mode=mode,
                                                use_cache=not force, log=STATE.say)
            except BaseException as e:
                r = {"ok": False, "error": repr(e)}
            with STATE.lock:
                item["llm"] = {
                    "status": "完成" if r.get("ok") else "失败",
                    "result": r if r.get("ok") else None,
                    "error": r.get("error"),
                    "problems": r.get("problems") or [],
                    "at": _now(),
                }
                STATE.rev += 1
            if r.get("ok"):
                STATE.say(f"  {len(r.get('elements') or [])} 个标签 · "
                          f"{r.get('stats', {}).get('eval_count')} tok · "
                          f"{r.get('stats', {}).get('tok_per_s')} tok/s · "
                          f"模式 {r.get('mode')} · "
                          f"{'部分结果' if r.get('partial') else '校验通过'}")
            else:
                STATE.say(f"  !! 失败：{r.get('error')}")
        with STATE.lock:
            STATE.llm["done"] = i + 1
            STATE.rev += 1

    with STATE.lock:
        STATE.llm.update({"running": False, "phase": "空闲", "current": None})
        STATE.rev += 1
    STATE.say("LLM 分析结束")


# ---------------------------------------------------------------- HTTP

CTYPE = {".html": "text/html; charset=utf-8", ".png": "image/png",
         ".jpg": "image/jpeg", ".webp": "image/webp", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "NAIReproGUI/1.0"

    def log_message(self, *a):
        pass

    # ---- helpers
    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def send_bytes(self, b, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def send_file(self, path):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            b = f.read()
        self.send_bytes(b, CTYPE.get(os.path.splitext(path)[1].lower(),
                                     "application/octet-stream"))

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def resolve_img(self, q):
        kind = q.get("kind", ["thumb"])[0]
        k = q.get("key", [""])[0]
        if kind == "thumb":
            p = os.path.join(THUMB_DIR, k + ".jpg")
        elif kind == "out":
            with STATE.lock:
                p = (STATE.items.get(k) or {}).get("out_png")
        elif kind == "bthumb":
            try:
                w = int(q.get("w", ["380"])[0])
            except (ValueError, TypeError):
                w = 380
            p = batch_thumb(q.get("id", [""])[0], q.get("f", [""])[0],
                            width=max(64, min(1600, w)))
        elif kind == "batch":
            p = os.path.join(batch_dir(q.get("id", [""])[0]), "images",
                             os.path.basename(q.get("f", [""])[0]))
        elif kind == "src":
            p = os.path.join(SRC_DIR, k + ".bin")
        else:
            p = None
        if not p:
            return None
        # 只允许访问 out 目录内的文件
        if os.path.commonpath([os.path.abspath(p), OUT]) != os.path.abspath(OUT):
            return None
        return p

    # ---- routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            self.send_file(os.path.join(HERE, "ui.html"))
        elif u.path == "/api/status":
            self.send_json(STATE.snapshot())
        elif u.path == "/api/urlsfile":
            p = os.path.join(HERE, "urls.txt")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    self.send_json({"ok": True, "text": f.read()})
            else:
                self.send_json({"ok": False, "error": "找不到 urls.txt"})
        elif u.path == "/api/common":
            keys = [k for k in (q.get("keys", [""])[0] or "").split(",") if k]
            self.send_json({"ok": True, **compute_common_cached(keys or STATE.order)})

        elif u.path == "/api/llm/models":
            # 扫一遍本地端口，列出可直接选用的模型
            extra = []
            cur = STATE.llm_cfg
            if cur.get("base_url") and cur["base_url"] not in [
                    b for _k, b in llm_analyze.PROBE_TARGETS]:
                extra.append((cur.get("backend") or "ollama", cur["base_url"]))
            profiles = llm_analyze.discover(extra)
            self.send_json({
                "ok": True,
                "profiles": profiles,
                "active": llm_analyze.match_profile(cur, profiles),
                "cfg": {k: v for k, v in cur.items()
                        if k in ("backend", "base_url", "model")},
            })

        elif u.path == "/api/llm/status":
            ok, msg = llm_analyze.check(STATE.llm_cfg)
            self.send_json({"ok": True, "ready": ok, "message": msg,
                            "cfg": {k: v for k, v in STATE.llm_cfg.items()
                                    if k in ("backend", "base_url", "model")},
                            "categories": llm_analyze.CATEGORIES})

        elif u.path == "/api/batches":
            db = "day_" + day_key()
            b0, b1 = day_window()
            self.send_json({"ok": True, "batches": list_batches(),
                            "mode": STATE.batch_mode, "last": STATE.last_batch,
                            "day": {"key": day_key(), "label": day_label(),
                                    "batch": db,
                                    "exists": read_batch(db) is not None,
                                    "start": int(b0), "end": int(b1),
                                    "hour": DAY_START_HOUR}})

        elif u.path == "/api/batches/detail":
            g = batch_gallery(q.get("id", [""])[0])
            if not g:
                self.send_json({"ok": False, "error": "批次不存在"})
            else:
                self.send_json({"ok": True, **g})

        elif u.path == "/api/prompts":
            k = q.get("key", [""])[0]
            with STATE.lock:
                pr = (STATE.items.get(k) or {}).get("prompts")
                base = STATE.orig_prompts.get(k)
            if pr is None:
                self.send_json({"ok": False, "error": "没有这个作品"})
            else:
                self.send_json({"ok": True, "prompts": pr,
                                "edited": bool(base is not None and base != pr)})
        elif u.path == "/img":
            p = self.resolve_img(q)
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

        if u.path == "/api/settings":
            with STATE.lock:
                STATE.settings.update({k: v for k, v in data.items()
                                       if k in DEFAULT_SETTINGS})
                tok = data.get("token")
                if tok:
                    STATE.token = tok
                snap = {"settings": dict(STATE.settings), "has_token": bool(STATE.token)}
                STATE.rev += 1
            self.send_json({"ok": True, **snap})

        elif u.path == "/api/parse":
            if STATE.running:
                self.send_json({"ok": False, "error": "有任务在跑"}, 409)
                return
            srcs = data.get("sources") or []
            if not srcs:
                self.send_json({"ok": False, "error": "没有数据源"}, 400)
                return
            if data.get("reset"):
                with STATE.lock:
                    STATE.items.clear()
                    STATE.raw.clear()
                    STATE.order.clear()
            threading.Thread(target=job_parse, args=(srcs,), daemon=True).start()
            self.send_json({"ok": True, "count": len(srcs)})

        elif u.path == "/api/run":
            if STATE.running:
                self.send_json({"ok": False, "error": "有任务在跑"}, 409)
                return
            if not STATE.token:
                self.send_json({"ok": False, "error": "缺少 Token"}, 400)
                return
            keys = data.get("keys") or list(STATE.order)
            if not keys:
                self.send_json({"ok": False, "error": "没有可生成的作品"}, 400)
                return
            threading.Thread(target=job_run, args=(keys,), daemon=True).start()
            self.send_json({"ok": True, "count": len(keys)})

        elif u.path == "/api/analyze":
            text = data.get("text") or ""
            if not text.strip():
                self.send_json({"ok": False, "error": "没有内容"}, 400)
                return
            try:
                an = analyze.analyze(text)
            except Exception as e:
                self.send_json({"ok": False, "error": f"解析失败：{e}"}, 400)
                return
            self.send_json({"ok": True, "analysis": an})

        elif u.path == "/api/batches/new":
            with STATE.lock:
                bid, b = create_batch(data.get("name") or None, STATE.settings)
                if data.get("select", True):
                    STATE.batch_mode = bid
                STATE.rev += 1
            self.send_json({"ok": True, "id": bid, "batch": b})

        elif u.path == "/api/batches/mode":
            mode = data.get("mode") or "auto"
            if mode != "auto" and not read_batch(mode):
                self.send_json({"ok": False, "error": "批次不存在"}, 400)
                return
            with STATE.lock:
                STATE.batch_mode = mode
                STATE.rev += 1
            self.send_json({"ok": True, "mode": mode})

        elif u.path == "/api/batches/rename":
            bid = data.get("id")
            b = read_batch(bid)
            if not b:
                self.send_json({"ok": False, "error": "批次不存在"}, 400)
                return
            b["name"] = (data.get("name") or "").strip() or b["name"]
            write_batch(bid, b)
            self.send_json({"ok": True, "name": b["name"]})

        elif u.path == "/api/batches/delete":
            bid = data.get("id")
            b = read_batch(bid)
            if not b:
                self.send_json({"ok": False, "error": "批次不存在"}, 400)
                return
            if not data.get("confirm"):
                self.send_json({"ok": False, "error": "需要确认"}, 400)
                return
            d = batch_dir(bid)
            if os.path.commonpath([os.path.abspath(d), BATCH_ROOT]) != os.path.abspath(BATCH_ROOT):
                self.send_json({"ok": False, "error": "路径非法"}, 400)
                return
            shutil.rmtree(d, ignore_errors=True)
            with STATE.lock:
                if STATE.batch_mode == bid:
                    STATE.batch_mode = "auto"
                if STATE.last_batch == bid:
                    STATE.last_batch = None
                STATE.rev += 1
            self.send_json({"ok": True})

        elif u.path == "/api/batches/open":
            d = batch_dir(data.get("id") or "")
            if not os.path.isdir(d):
                self.send_json({"ok": False, "error": "批次不存在"}, 400)
                return
            try:
                os.startfile(d)          # noqa: S606  (仅 Windows 本地工具)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif u.path == "/api/prompts/set":
            k = data.get("key")
            pr = data.get("prompts")
            with STATE.lock:
                it = STATE.items.get(k)
                if not it or not isinstance(pr, dict):
                    self.send_json({"ok": False, "error": "参数不对"}, 400)
                    return
                it["prompts"] = pr
                it["edited"] = STATE.orig_prompts.get(k) != pr
                STATE.prompt_rev += 1
                STATE.rev += 1
            self.send_json({"ok": True})

        elif u.path == "/api/prompts/reset":
            keys = data.get("keys") or list(STATE.orig_prompts.keys())
            n = 0
            with STATE.lock:
                for k in keys:
                    if k in STATE.orig_prompts and k in STATE.items:
                        STATE.items[k]["prompts"] = copy.deepcopy(STATE.orig_prompts[k])
                        STATE.items[k]["edited"] = False
                        n += 1
                STATE.prompt_rev += 1
                STATE.rev += 1
            self.send_json({"ok": True, "count": n})

        elif u.path == "/api/prompts/replace":
            find = data.get("find") or ""
            if not find:
                self.send_json({"ok": False, "error": "查找内容为空"}, 400)
                return
            repl = data.get("replace") or ""
            use_rx = bool(data.get("regex"))
            ic = bool(data.get("ignore_case"))
            targets = [t for t in (data.get("targets") or ["pos_base", "pos_chars"])
                       if t in TARGETS]
            if not targets:
                self.send_json({"ok": False, "error": "没有勾选作用范围"}, 400)
                return
            keys = data.get("keys") or list(STATE.order)
            apply_ = bool(data.get("apply"))
            cats_filter = [c for c in (data.get("cats") or []) if c]
            try:
                re.compile(find if use_rx else re.escape(find),
                           re.IGNORECASE if ic else 0)
            except re.error as e:
                self.send_json({"ok": False, "error": f"正则错误：{e}"}, 400)
                return

            total, n, detail = 0, 0, []
            with STATE.lock:
                for k in keys:
                    it = STATE.items.get(k)
                    if not it or not isinstance(it.get("prompts"), dict):
                        continue
                    if apply_:
                        pr, hits = do_replace(it["prompts"], find, repl, use_rx, ic,
                                              targets, cats_filter)
                        it["prompts"] = pr
                        it["edited"] = STATE.orig_prompts.get(k) != pr
                    else:
                        hits = count_hits(it["prompts"], find, use_rx, ic,
                                          targets, cats_filter)
                    if hits:
                        total += hits
                        n += 1
                        detail.append({"key": k, "name": it.get("name"), "hits": hits})
                if apply_:
                    STATE.prompt_rev += 1
                STATE.rev += 1
            self.send_json({"ok": True, "applied": apply_, "hits": total,
                            "items": n, "detail": detail[:40]})

        elif u.path == "/api/llm/config":
            # 只切换配置，不启动分析
            for k in ("backend", "base_url", "model"):
                v = (data.get(k) or "").strip()
                if v:
                    STATE.llm_cfg[k] = v.rstrip("/") if k == "base_url" else v
            try:
                with open(llm_analyze.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump({k: v for k, v in STATE.llm_cfg.items()
                               if not k.startswith("_")}, f,
                              ensure_ascii=False, indent=2)
            except Exception as e:
                STATE.say(f"配置写入失败: {e}")
            ok, msg = llm_analyze.check(STATE.llm_cfg)
            STATE.say(f"切换模型：{STATE.llm_cfg['model']} — {msg}")
            with STATE.lock:
                STATE.rev += 1
            self.send_json({"ok": True, "ready": ok, "message": msg,
                            "cfg": {k: v for k, v in STATE.llm_cfg.items()
                                    if k in ("backend", "base_url", "model")}})

        elif u.path == "/api/llm/analyze":
            if STATE.llm["running"]:
                self.send_json({"ok": False, "error": "已有分析任务在跑"}, 409)
                return
            # 允许前端覆盖 backend / base_url / model 并写回配置
            changed = False
            for k in ("backend", "base_url", "model"):
                v = (data.get(k) or "").strip()
                if v and v != STATE.llm_cfg.get(k):
                    STATE.llm_cfg[k] = v.rstrip("/") if k == "base_url" else v
                    changed = True
            if changed:
                try:
                    with open(llm_analyze.CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump({k: v for k, v in STATE.llm_cfg.items()
                                   if not k.startswith("_")},
                                  f, ensure_ascii=False, indent=2)
                except Exception as e:
                    STATE.say(f"配置写入失败: {e}")

            keys = data.get("keys") or list(STATE.order)
            keys = [k for k in keys if k]
            if not keys:
                self.send_json({"ok": False, "error": "没有可分析的作品"}, 400)
                return
            mode = data.get("mode") or STATE.settings.get("llm_mode") or "full"
            if data.get("mode"):
                with STATE.lock:
                    STATE.settings["llm_mode"] = mode
            threading.Thread(target=job_llm,
                             args=(keys, bool(data.get("force")), mode),
                             daemon=True).start()
            est = 70 if mode == "full" else 33
            self.send_json({"ok": True, "count": len(keys), "mode": mode,
                            "note": f"约 {est} 秒/张，可在日志里看进度"})

        elif u.path == "/api/stop":
            with STATE.lock:
                STATE.cancel = True
                STATE.rev += 1
            self.send_json({"ok": True})

        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description="NAI 复现工具 GUI")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    a = ap.parse_args()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://{a.host}:{a.port}/"
    print(f"NAI 复现工具 GUI  ->  {url}")
    print(f"输出目录: {OUT}")
    if not STATE.token:
        print("提示: 未检测到 NAI_TOKEN 环境变量，可在界面里填 Token")
    if a.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
