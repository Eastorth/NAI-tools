#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAI 提示词分析模块

做三件事：
  1. 解析权重语法   [[[tag]]] / {{tag}} / 1.5::tag:: / -1::tag::
  2. 结构分段       按 "." 把提示词切成若干段（保护 1.5 这类小数）
  3. 语义归类       画师 / 美术风格 / 质量 / 场景背景 / 视角空间 / 角色 / 元信息

词典内置一份常用词，可用 categories.json 覆盖扩充。

    python analyze.py "<提示词>"
    python analyze.py --text-file prompt.txt --json out.json
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CATEGORY_FILE = os.path.join(HERE, "categories.json")

UNKNOWN = "未分类"
NEGATIVE = "负面"

# 归类优先级：越靠前越优先
ORDER = ["元信息", "质量", "视角/空间", "场景/背景", "角色", "美术风格", "画师"]

BUILTIN = {
    "元信息": [
        "ai-assisted", "ai generated", "generated", "novelai",
        "year 2023", "year 2024", "year 2025", "year 2026",
    ],
    "质量": [
        "masterpiece", "best quality", "amazing quality", "very aesthetic",
        "perfect quality", "great quality", "highres", "absurdres", "ultra detailed",
        "highly detailed", "detailed", "amazing detail", "no text", "text",
        "perfectly drawn", "8k", "4k", "sharp focus", "official art", "good quality",
        "normal quality", "aesthetic", "very awa", "worst quality", "low quality",
        "lowres", "upscaled", "jpeg artifacts", "scan artifacts", "watermark",
        "signature", "artist name", "logo",
    ],
    "视角/空间": [
        "from below", "from above", "from side", "from behind", "from outside",
        "front view", "back view", "side view", "three quarter view", "profile",
        "cowboy shot", "wide shot", "close-up", "extreme close-up", "full body",
        "upper body", "lower body", "portrait", "face focus", "headshot",
        "looking at viewer", "looking away", "looking back", "looking down",
        "looking up", "dutch angle", "pov", "foreshortening", "perspective",
        "vanishing point", "bird's eye view", "worm's eye view", "fisheye",
        "panorama", "scenery focus", "tilted head", "straight-on",
        "medium shot", "long shot", "zoom in", "zoom out", "depth",
    ],
    "场景/背景": [
        "indoors", "outdoors", "locker room", "lockers", "bench", "wood bench",
        "tile floor", "floor", "wall", "window", "classroom", "bedroom", "kitchen",
        "bathroom", "school", "hallway", "corridor", "stairs", "rooftop", "city",
        "street", "night", "day", "sunset", "sunrise", "sky", "clouds", "forest",
        "grass", "beach", "ocean", "mountain", "room", "building", "garden", "park",
        "library", "cafe", "restaurant", "train", "car", "door", "table", "chair",
        "bed", "curtain", "plant", "tree", "flowers", "water", "rain", "snow",
        "sunlight", "shadow", "background", "simple background", "white background",
        "blurry background", "grey background", "gradient background", "ceiling",
        "bookshelf", "desk", "mirror", "bathtub", "shower", "pool", "field", "hill",
    ],
    "角色": [
        "1girl", "2girls", "3girls", "1boy", "2boys", "3boys", "multiple girls",
        "multiple boys", "solo", "duo", "trio", "group", "male focus", "female focus",
        "solo focus", "girl", "boy", "man", "woman", "male", "female",
    ],
    "美术风格": [
        "flat color", "flat colour", "anime style", "anime coloring", "realistic",
        "sketch", "watercolor", "oil painting", "cel shading", "lineart",
        "monochrome", "greyscale", "grayscale", "chromatic aberration", "vignette",
        "blurry", "muted color", "vibrant color", "pastel color", "soft lighting",
        "dramatic lighting", "cinematic lighting", "backlighting", "rim light",
        "depth of field", "bokeh", "motion blur", "glowing", "gradient",
        "photorealistic", "impressionism", "ukiyo-e", "pixel art",
    ],
    "画师": [],
}


# ---------------------------------------------------------------- 词典

def load_categories(path=None):
    path = path or CATEGORY_FILE
    d = {k: {x.lower() for x in v} for k, v in BUILTIN.items()}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                extra = json.load(f)
            for k, v in extra.items():
                if k.startswith("_") or isinstance(v, str):
                    continue          # "_说明" 之类的注释键
                d.setdefault(k, set()).update(str(x).lower() for x in v)
        except Exception as e:
            print(f"[warn] 读取 {path} 失败: {e}", file=sys.stderr)
    return d


def classify(text, cats):
    """返回分类名，找不到返回 None。"""
    t = (text or "").strip().lower()
    if not t:
        return None
    for c in ORDER:
        if t in cats.get(c, ()):
            return c
    # 词边界回退：长词优先，避免 "bench" 抢走 "wood bench"
    cands = []
    for c in ORDER:
        for kw in cats.get(c, ()):
            if len(kw) >= 4:
                cands.append((len(kw), c, kw))
    for _, c, kw in sorted(cands, reverse=True):
        if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", t):
            return c
    return None


# ---------------------------------------------------------------- 权重语法

def bracket_weight(raw):
    """解析 [[[tag]]] / {{tag}}，返回 (裸文本, 权重, 裸文本左侧偏移)。"""
    lead = len(raw) - len(raw.lstrip())
    t = raw.strip()
    ltrim = 0
    w = 1.0

    m = re.match(r"^(\{+)(.*?)(\}+)$", t, re.S)
    if m:
        n = len(m.group(1))
        w *= 1.05 ** n
        t = m.group(2)
        ltrim += n

    m = re.match(r"^(\[+)(.*?)(\]+)$", t, re.S)
    if m:
        n = len(m.group(1))
        w /= 1.05 ** n
        t = m.group(2)
        ltrim += n

    t2 = t.strip()
    ltrim += len(t) - len(t2)
    return t2, round(w, 6), lead + ltrim


WEIGHT_GROUP = re.compile(r"^(\s*)([-+]?\d*\.?\d+)::(.*)::(\s*)$", re.S)
WEIGHT_TAIL = re.compile(r"([-+]?\d*\.?\d+)::(.*)::(\s*)$", re.S)


def iter_chunks(text):
    """按 ',' '.' '换行' 切分，跳过 ::...:: 内部，保护 1.5 这类小数。

    产出 (chunk, start, end, sep)，sep 是紧随其后的分隔符。
    """
    i, n, start, depth = 0, len(text), 0, 0
    while i < n:
        if text.startswith("::", i):
            depth = 1 - depth
            i += 2
            continue
        ch = text[i]
        if depth == 0 and (ch in ",\r\n" or ch == "."):
            if ch == ".":
                prev = text[i - 1] if i > 0 else ""
                nxt = text[i + 1] if i + 1 < n else ""
                if prev.isdigit() and nxt.isdigit():   # 1.5 之类，不当分隔符
                    i += 1
                    continue
            yield text[start:i], start, i, ch
            start = i + 1
            i += 1
            continue
        i += 1
    if start < n:
        yield text[start:n], start, n, None


def _emit_plain(chunk, off, base_w, seg, out):
    t, w, ltrim = bracket_weight(chunk)
    if not t:
        return
    ts = off + ltrim
    # rstart/rend 覆盖含权重括号在内的整块，便于跨图高亮
    lead = len(chunk) - len(chunk.lstrip())
    tail = len(chunk) - len(chunk.rstrip())
    out.append({
        "raw": chunk,
        "text": t,
        "weight": round(base_w * w, 4),
        "start": ts,
        "end": ts + len(t),
        "rstart": off + lead,
        "rend": off + len(chunk) - tail,
        "segment": seg,
    })


def _parse_chunk(chunk, off, base_w, seg, out):
    """一个 chunk 可能是：普通标签 / 权重组 / 权重组后面还跟着残余文本。"""
    m = WEIGHT_GROUP.match(chunk)
    if not m:
        # 形如 "no text -1:: flat color::"，权重组前面还有普通标签
        m = WEIGHT_TAIL.search(chunk)
        if m and m.start(1) > 0:
            _emit_plain(chunk[:m.start(1)], off, base_w, seg, out)
        elif m:
            pass
        else:
            _emit_plain(chunk, off, base_w, seg, out)
            return
    if m:
        w = float(m.group(1) if m.re is WEIGHT_TAIL else m.group(2))
        gi = 2 if m.re is WEIGHT_TAIL else 3
        inner = m.group(gi)
        inner_off = off + m.start(gi)
        for c, s, _e, _sep in iter_chunks(inner):
            _parse_chunk(c, inner_off + s, base_w * w, seg, out)


def parse_tags(text, cats=None):
    out = []
    seg = 0
    for chunk, s, _e, sep in iter_chunks(text):
        _parse_chunk(chunk, s, 1.0, seg, out)
        if sep == ".":
            seg += 1
    return out


def split_segments(text):
    """按 '.' 分段，跳过 ::...:: 内部，并保护 1.5 这类小数。"""
    segs, i, n, start, depth = [], 0, len(text), 0, 0
    while i < n:
        if text.startswith("::", i):
            depth = 1 - depth
            i += 2
            continue
        if text[i] == "." and depth == 0:
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if not (prev.isdigit() and nxt.isdigit()):
                segs.append((start, i))
                start = i + 1
        i += 1
    segs.append((start, n))
    return segs


# ---------------------------------------------------------------- 分析

def analyze(text, cats=None):
    cats = cats or load_categories()
    tags = parse_tags(text, cats)
    segs = split_segments(text)

    # 首个「场景/视角」标签的位置，用来判断前面的短词是不是画师
    first_space_cat = None
    for i, tg in enumerate(tags):
        if classify(tg["text"], cats) in ("场景/背景", "视角/空间"):
            first_space_cat = i
            break

    for i, tg in enumerate(tags):
        c = classify(tg["text"], cats)
        tg["guess"] = False
        if c is None:
            t = tg["text"]
            looks_like_name = (" " not in t) or ("(" in t) or ("-" in t)
            if (first_space_cat is not None and i < first_space_cat
                    and looks_like_name and not any(ch.isdigit() for ch in t)
                    and 1 < len(t) <= 30):
                c = "画师"
                tg["guess"] = True
            else:
                c = UNKNOWN
        tg["cat"] = c
        tg["negative"] = tg["weight"] < 0

    # 汇总
    by_cat = {}
    for tg in tags:
        by_cat.setdefault(tg["cat"], []).append(tg)

    counts = {k: len(v) for k, v in by_cat.items()}
    weighted = [t for t in tags if abs(t["weight"] - 1.0) > 1e-9]
    negatives = [t for t in tags if t["negative"]]

    seg_info = []
    for i, (a, b) in enumerate(segs):
        raw = text[a:b]
        inner = [t for t in tags if t["segment"] == i]
        cats_here = {}
        for t in inner:
            cats_here[t["cat"]] = cats_here.get(t["cat"], 0) + 1
        dom = max(cats_here.items(), key=lambda kv: kv[1])[0] if cats_here else UNKNOWN
        seg_info.append({
            "index": i,
            "text": raw,
            "trimmed": raw.strip().strip(",").strip(),
            "tags": len(inner),
            "dominant": dom,
            "counts": cats_here,
        })

    return {
        "text": text,
        "chars": len(text),
        "tag_count": len(tags),
        "tags": tags,
        "segments": seg_info,
        "counts": counts,
        "by_cat": {k: [t["text"] for t in v] for k, v in by_cat.items()},
        "weighted": len(weighted),
        "negative": len(negatives),
        "unknown": [t["raw"].strip() for t in tags if t["cat"] == UNKNOWN],
    }


# ---------------------------------------------------------------- 按分类替换

def replace_in_categories(text, find, repl, cats_filter, use_regex=False,
                          ignore_case=False, cats=None):
    """只在指定分类的标签内做替换，其余部分逐字节保留。"""
    cats = cats or load_categories()
    tags = parse_tags(text, cats)
    for tg in tags:
        c = classify(tg["text"], cats)
        tg["cat"] = c if c else UNKNOWN

    flags = re.IGNORECASE if ignore_case else 0
    pat = re.compile(find if use_regex else re.escape(find), flags)

    hits = 0
    out = text
    # 从后往前替换，避免偏移失效
    for tg in sorted(tags, key=lambda x: -x["start"]):
        if tg["cat"] not in cats_filter:
            continue
        new, n = pat.subn(repl, tg["text"])
        if n:
            out = out[:tg["start"]] + new + out[tg["end"]:]
            hits += n
    return out, hits


# ---------------------------------------------------------------- CLI

def render(an):
    W = 78
    print("=" * W)
    print(f"标签 {an['tag_count']} 个 · 加权 {an['weighted']} 个 · 负权重 {an['negative']} 个 "
          f"· 共 {an['chars']} 字")
    print("=" * W)

    print("\n【按语义分类】")
    for c in ORDER + [UNKNOWN]:
        items = an["by_cat"].get(c)
        if not items:
            continue
        print(f"\n  ▸ {c}  ({len(items)})")
        line = "      "
        for t in items:
            tg = next(x for x in an["tags"] if x["text"] == t)
            mark = "?"
            if abs(tg["weight"] - 1.0) > 1e-9:
                mark = f"×{tg['weight']:g}"
            if tg["negative"]:
                mark += " ✗"
            piece = f"{t}[{mark}] " if mark != "?" else f"{t} "
            if len(line) + len(piece) > W:
                print(line)
                line = "      "
            line += piece
        if line.strip():
            print(line)

    print("\n【按 '.' 分段】")
    for s in an["segments"]:
        body = s["trimmed"]
        if len(body) > 150:
            body = body[:150] + "…"
        print(f"\n  段{s['index']}  ({s['tags']} 标签 · 主分类: {s['dominant']})")
        print(f"      {body}")

    if an["unknown"]:
        print(f"\n【未分类 {len(an['unknown'])} 个】")
        print("      " + ", ".join(an["unknown"]))
        print(f"      可写入 {CATEGORY_FILE} 扩充词典，例如：")
        print('      { "画师": ["你认识的画师名"] }')
    print()


def main():
    ap = argparse.ArgumentParser(description="NAI 提示词分析")
    ap.add_argument("text", nargs="?", help="提示词")
    ap.add_argument("--text-file", help="从文件读取提示词")
    ap.add_argument("--json", help="把分析结果写到 JSON")
    ap.add_argument("--cats", help="自定义词典路径")
    a = ap.parse_args()

    text = a.text
    if a.text_file:
        with open(a.text_file, encoding="utf-8") as f:
            text = f.read()
    if not text:
        text = sys.stdin.read()
    if not text.strip():
        raise SystemExit("没有提示词。用法: python analyze.py \"<提示词>\"")

    an = analyze(text, load_categories(a.cats))
    render(an)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(an, f, ensure_ascii=False, indent=2)
        print(f"已写出 {a.json}")


if __name__ == "__main__":
    main()
