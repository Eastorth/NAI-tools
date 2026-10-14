#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAI 作品复现工具
================
从 aitag 图床的 WebP（或本地 PNG/WebP/JSON）中提取 NovelAI 原始出图参数，
按指定模型与采样参数重新提交到 NovelAI 官方 API，并把结果与原始参数逐字段核对。

用法示例
--------
# 只构建请求、逐字段核对，不消耗额度
python repro.py <url|file> --dry-run

# 用 V5 / Euler a / 1280x768 / 23 步 / CFG 7 实跑
python repro.py <url|file> --model nai-diffusion-5-full \
    --sampler k_euler_ancestral --size 1280x768 --steps 23 --scale 7

# 换掉提示词做链路验证（不动角色提示词，用 --drop-characters 清空）
python repro.py <url|file> --prompt "..." --drop-characters

Token 从环境变量 NAI_TOKEN 读取，或 --token 传入；永远不会被打印。
"""

import argparse
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

from PIL import Image, ExifTags

# ---------------------------------------------------------------- 常量

IMG_API = "https://image.novelai.net/ai/generate-image"
DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_REFERER = "https://aitag.win/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 服务端生成 / 仅用于记录的字段，回传会污染请求
SERVER_ONLY_KEYS = {
    "signed_hash", "model_hash", "model_name", "request_type",
    "version", "extra_passthrough_testing",
}

# 需要参与核对的字段
CHECK_KEYS = [
    "steps", "width", "height", "scale", "seed", "sampler", "noise_schedule",
    "cfg_rescale", "uncond_scale", "sm", "sm_dyn", "dynamic_thresholding",
    "skip_cfg_above_sigma", "skip_cfg_below_sigma", "prefer_brownian",
    "deliberate_euler_ancestral_bug", "quality_boost", "legacy_v3_extend",
    "controlnet_model", "lora_unet_weights",
]

# NovelAI Director（参考图）相关数组。参考图本体从不写进元数据，
# 但配套的描述 / 提取度 / 强度会保留下来，直接回传必然校验失败。
DIRECTOR_KEYS = (
    "director_reference_images",
    "director_reference_descriptions",
    "director_reference_information_extracted",
    "director_reference_strengths",
    "director_reference_secondary_strengths",
)


def director_usage(comment):
    """返回非空的 Director 数组名（不含 images）。用于事前预警。"""
    if not isinstance(comment, dict):
        return []
    return [k for k in DIRECTOR_KEYS[1:]
            if isinstance(comment.get(k), list) and len(comment[k])]


def conditioning_usage(comment):
    """扫描原图参数里一切「图像条件」痕迹，用于事前预警。"""
    if not isinstance(comment, dict):
        comment = {}
    v = {
        "director": director_usage(comment),
        "reference": [k for k in ("reference_information_extracted_multiple",
                                  "reference_strength_multiple")
                      if isinstance(comment.get(k), list) and comment[k]],
        "img2img": bool(comment.get("img2img")),
        "add_original_image": bool(comment.get("add_original_image")),
        "controlnet": bool(comment.get("controlnet_model")),
        "request_type": comment.get("request_type"),
    }
    v["any"] = bool(v["director"] or v["reference"] or v["img2img"]
                    or v["add_original_image"] or v["controlnet"]
                    or v["request_type"] == "NativeInfillingRequest")
    return v


# 一切「图像条件」字段的中性值。元数据里从不包含这些功能的图像本体
# （参考图、原图、蒙版），留着只会被服务端以数组长度不匹配之类的理由拒绝，
# 或者悄悄产出一张与目标无关的图。
NEUTRALIZE = {
    "director_reference_images": None,
    "director_reference_descriptions": None,
    "director_reference_information_extracted": None,
    "director_reference_strengths": None,
    "director_reference_secondary_strengths": None,
    "reference_information_extracted_multiple": [],
    "reference_strength_multiple": [],
    "img2img": None,
    "add_original_image": False,
    "controlnet_model": None,
    "controlnet_strength": None,
    "strength": None,
    "noise": None,
    "extra_noise_seed": None,
    "mask": None,
    "image": None,
    # NovelAI 的 extra_passthrough_testing 家族偶尔会漏到 Comment 顶层。
    # 其中 upscaled_enhance 只允许在 img2img 请求上用，纯文生图带上必然 400。
    "upscaled_enhance": None,
    "upscale": None,
    "original_width": None,
    "original_height": None,
    "crop_top": None,
    "crop_left": None,
    "pd_grid": None,
    "crash_hydra": False,
    "hide_debug_overlay": False,
}

# 服务端校验报错里提到的字段名，形如：
#   Validation error: upscaled_enhance is only supported on img2img requests
FIELD_ERR = re.compile(r"Validation error:\s*([A-Za-z_][A-Za-z0-9_]*)")


def normalize_txt2img(p, trace=None):
    """强制纯文生图，返回被改动的字段名列表。

    只处理原参数里**确实存在**的字段，不会凭空添加。
    """
    changed = []
    for k, neutral in NEUTRALIZE.items():
        if k not in p:
            continue
        cur = p[k]
        if cur == neutral:
            continue
        if isinstance(neutral, list) and isinstance(cur, list) and not cur:
            continue
        changed.append((k, cur))
    for k, _old in changed:
        p[k] = NEUTRALIZE[k]
    if trace is not None:
        for k, old in changed:
            brief = json.dumps(old, ensure_ascii=False, default=str)
            if len(brief) > 48:
                brief = brief[:45] + "..."
            trace.append((k, brief, json.dumps(NEUTRALIZE[k]),
                          "强制纯文生图：去掉参考图 / 局部重绘"))
    return [k for k, _ in changed]


# ---------------------------------------------------------------- 输出路径

def unique_path(path):
    """文件已存在时追加 -2 / -3 …，绝不覆盖已有结果。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{root}-{i}{ext}"):
        i += 1
    return f"{root}-{i}{ext}"


def out_dirs(base):
    d = {k: os.path.join(base, k) for k in ("images", "requests", "meta")}
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    return d


# ---------------------------------------------------------------- 取数层

def http_get(url, proxy=None, referer=None, timeout=90):
    opener = _opener(proxy)
    r = urllib.request.Request(url, method="GET")
    r.add_header("User-Agent", UA)
    if referer:
        r.add_header("Referer", referer)
    with opener.open(r, timeout=timeout) as resp:
        return resp.read()


def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _clean_usercomment(raw):
    """EXIF UserComment 形如 b'ASCII\\x00\\x00\\x00{...}'，剥掉前缀取 JSON。"""
    if isinstance(raw, bytes):
        i = raw.find(b"{")
        if i < 0:
            return None
        raw = raw[i:]
        txt = raw.decode("utf-8", "replace")
    else:
        i = raw.find("{")
        if i < 0:
            return None
        txt = raw[i:]
    txt = txt.rstrip("\x00").strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        # 有些导出会在 JSON 后追加填充，尝试取到最后一个 }
        j = txt.rfind("}")
        if j > 0:
            try:
                return json.loads(txt[:j + 1])
            except json.JSONDecodeError:
                return None
    return None


def extract_params(blob, label="input"):
    """从图片字节流里挖出 NAI 参数 JSON，返回 (params, 来源说明)。"""
    im = Image.open(io.BytesIO(blob))

    # 1) WebP：EXIF UserComment（aitag 图床走这条）
    try:
        exif = im.getexif()
        sub = exif.get_ifd(0x8769)
        uc = sub.get(0x9286)
        if uc:
            params = _clean_usercomment(uc)
            if params:
                return params, "EXIF/UserComment"
    except Exception:
        pass

    # 2) PNG：Comment 块
    for key in ("Comment", "comment", "parameters"):
        v = im.info.get(key)
        if v:
            params = _clean_usercomment(v)
            if params:
                return params, f"PNG/{key}"

    # 3) 兜底：只有提示词
    desc = im.info.get("Description") or im.info.get("ImageDescription")
    if desc:
        if isinstance(desc, bytes):
            desc = desc.decode("utf-8", "replace")
        return ({"prompt": desc, "v4_prompt": {"caption": {"base_caption": desc,
                                                           "char_captions": []}}},
                "图片 Description（仅提示词，参数不完整）")

    raise SystemExit(f"[{label}] 没有找到任何 NAI 元数据")


def fetch_image(url, proxy, referer):
    """图床对 Referer 有硬性要求，按优先级逐个回退。"""
    host_root = "/".join(url.split("/")[:3]) + "/"
    tries = []
    for r in (referer, DEFAULT_REFERER, host_root, None):
        if r not in tries:
            tries.append(r)

    last = None
    for r in tries:
        try:
            return http_get(url, proxy=proxy, referer=r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (401, 403):
                raise
    raise SystemExit(f"抓取失败（已尝试 {len(tries)} 种 Referer）-> {last}")


def image_source(blob, params=None):
    """统一取「出图模型」。

    WebP 走 EXIF UserComment，外层 JSON 里自带 Source；
    PNG 走 Comment 块，返回的是内层参数对象，Source 在文本块里。
    """
    if isinstance(params, dict) and params.get("Source"):
        return params["Source"]
    try:
        im = Image.open(io.BytesIO(blob))
        return im.info.get("Source") or im.info.get("Software")
    except Exception:
        return None


def load_source(src, proxy, referer):
    """src 可以是 http(s) 图片地址、本地图片路径、或 .json 参数文件。"""
    if src.lower().startswith(("http://", "https://")):
        name = os.path.basename(src.split("?")[0]) or "remote"
        blob = fetch_image(src, proxy, referer)
    else:
        with open(src, "rb") as f:
            blob = f.read()
        name = os.path.basename(src)

    if src.lower().endswith(".json") or blob[:1] == b"{":
        return json.loads(blob.decode("utf-8", "replace")), "JSON 文件", name, blob
    params, how = extract_params(blob, name)
    return params, how, name, blob


# ---------------------------------------------------------------- 归一化层

def build_v5_payload(orig, args):
    """把原始参数（V3/V4/V4.5 均可）改写成 V5 的请求体，返回 (payload, 覆盖记录)。"""
    comment = orig.get("Comment", orig) if isinstance(orig, dict) else orig
    if isinstance(comment, str):
        comment = json.loads(comment)

    p = {k: v for k, v in comment.items() if k not in SERVER_ONLY_KEYS}
    trace = []

    def override(key, value, note=""):
        old = p.get(key, "<缺失>")
        if old != value:
            trace.append((key, old, value, note))
        p[key] = value

    override("sampler", args.sampler, "命令行")
    override("steps", args.steps, "命令行")
    override("scale", args.scale, "命令行")
    if not getattr(args, "keep_size", False):
        override("width", args.size[0], "命令行")
        override("height", args.size[1], "命令行")
    if getattr(args, "random_seed", False):
        override("seed", random.randint(0, 4294967295), "随机种子")
    elif args.seed is not None:
        override("seed", args.seed, "命令行")

    if args.prompt is not None:
        v4 = p.setdefault("v4_prompt", {}).setdefault("caption", {})
        trace.append(("v4_prompt.base_caption", "(原始提示词)", args.prompt[:60] + "...", "命令行"))
        v4["base_caption"] = args.prompt
        p["prompt"] = args.prompt

    if args.drop_characters:
        for side in ("v4_prompt", "v4_negative_prompt"):
            cap = p.get(side, {}).get("caption", {})
            if cap.get("char_captions"):
                trace.append((f"{side}.char_captions",
                              f"{len(cap['char_captions'])} 条", "已清空", "--drop-characters"))
                cap["char_captions"] = []

    if args.noise_schedule:
        override("noise_schedule", args.noise_schedule, "命令行")

    for k in ("v4_prompt", "v4_negative_prompt"):
        if k in p and not isinstance(p[k], dict):
            p[k] = json.loads(p[k]) if isinstance(p[k], str) else p[k]

    # 默认强制纯文生图：去掉参考图 / img2img / 局部重绘 / ControlNet
    if not getattr(args, "keep_image_refs", False):
        normalize_txt2img(p, trace)

    base = p.get("v4_prompt", {}).get("caption", {}).get("base_caption") or p.get("prompt", "")

    payload = {
        "input": base,
        "model": args.model,
        "action": "generate",
        "parameters": p,
    }
    return payload, trace


# ---------------------------------------------------------------- 提交层

def submit(payload, token, proxy, retries=3, trace=None):
    """提交请求。遇到「某字段不被支持」这类 400 时，自动置空该字段并重试。

    这样即使以后冒出我没枚举到的 passthrough 字段，也能自己救回来。
    """
    body = json.dumps(payload).encode("utf-8")
    params = payload.get("parameters") or {}
    auto_fixed = []
    last = None
    attempt = 0

    while attempt < retries + 3:
        attempt += 1
        r = urllib.request.Request(IMG_API, data=body, method="POST")
        r.add_header("Authorization", "Bearer " + token)
        r.add_header("Content-Type", "application/json")
        r.add_header("User-Agent", UA)
        try:
            with _opener(proxy).open(r, timeout=300) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            last = f"HTTP {e.code}: {detail[:600]}"

            # 400 且指明了字段名 —— 把它置空后原样重试
            if e.code == 400:
                m = FIELD_ERR.search(detail)
                if m:
                    f = m.group(1)
                    if f in params and params[f] is not None:
                        old = params[f]
                        params[f] = None
                        auto_fixed.append(f)
                        brief = json.dumps(old, ensure_ascii=False, default=str)[:60]
                        print(f"  [自愈] 服务端拒绝字段 {f}={brief}，已置空后重试")
                        if trace is not None:
                            trace.append((f, brief, "null",
                                          "服务端校验拒绝，自动置空后重试"))
                        body = json.dumps(payload).encode("utf-8")
                        continue

            if e.code in (429, 500, 502, 503) and attempt < retries:
                wait = 5 * attempt
                print(f"  [{last}] -> {wait}s 后重试 ({attempt}/{retries})")
                time.sleep(wait)
                continue
            raise SystemExit(f"提交失败 -> {last}")
        except Exception as e:
            last = repr(e)
            if attempt < retries:
                time.sleep(5 * attempt)
                continue
            raise SystemExit(f"提交失败 -> {last}")
    raise SystemExit(f"提交失败 -> {last}")


def unpack_png(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = [n for n in z.namelist() if n.lower().endswith(".png")]
        if not names:
            raise SystemExit(f"zip 里没有 png: {z.namelist()}")
        return z.read(names[0])
    raise SystemExit(f"无法识别的响应体: {raw[:120]!r}")


# ---------------------------------------------------------------- 验收层

def ahash(png_bytes, side=16):
    """简易感知哈希，用于跨版本粗比对。"""
    try:
        import numpy as np
    except ImportError:
        return None
    im = Image.open(io.BytesIO(png_bytes)).convert("L").resize((side, side))
    a = np.asarray(im, dtype=float)
    bits = (a > a.mean()).flatten()
    return "".join("1" if b else "0" for b in bits)


def hamming(a, b):
    if not a or not b or len(a) != len(b):
        return None
    return sum(1 for x, y in zip(a, b) if x != y)


def compare(orig, sent_params, got_params):
    rows = []
    for k in CHECK_KEYS:
        o = orig.get(k, "<缺失>")
        s = sent_params.get(k, "<未发送>")
        g = got_params.get(k, "<缺失>")
        rows.append((k, o, s, g))

    prompt_rows = []
    for side in ("v4_prompt", "v4_negative_prompt"):
        oc = (orig.get(side) or {}).get("caption") or {}
        gc = (got_params.get(side) or {}).get("caption") or {}
        prompt_rows.append((
            f"{side}.base_caption",
            (oc.get("base_caption") or "")[:40],
            (gc.get("base_caption") or "")[:40],
            "一致" if oc.get("base_caption") == gc.get("base_caption") else "不同",
        ))
        ocaps = oc.get("char_captions") or []
        gcaps = gc.get("char_captions") or []
        prompt_rows.append((
            f"{side}.char_captions",
            f"{len(ocaps)} 条",
            f"{len(gcaps)} 条",
            "一致" if len(ocaps) == len(gcaps) else "数量不同",
        ))
        for i in range(max(len(ocaps), len(gcaps))):
            a = (ocaps[i].get("char_caption") if i < len(ocaps) else "") or ""
            b = (gcaps[i].get("char_caption") if i < len(gcaps) else "") or ""
            prompt_rows.append((
                f"  char[{i}]",
                a[:34] + ("..." if len(a) > 34 else ""),
                b[:34] + ("..." if len(b) > 34 else ""),
                "一致" if a == b else "不同",
            ))
    return rows, prompt_rows


# ---------------------------------------------------------------- 主流程

def process(args, src):
    print(f"\n{'=' * 74}\n源: {src}\n{'=' * 74}")
    orig, how, name, orig_blob = load_source(src, args.proxy, args.referer)
    stem = os.path.splitext(name)[0]
    print(f"元数据来源: {how}")

    payload, trace = build_v5_payload(orig, args)
    p = payload["parameters"]

    print(f"\n--- 请求参数（model={args.model}）---")
    for k in ("sampler", "steps", "scale", "width", "height", "seed",
              "noise_schedule", "cfg_rescale", "prefer_brownian",
              "deliberate_euler_ancestral_bug", "sm", "sm_dyn"):
        print(f"  {k:32s} = {p.get(k, '<默认>')}")

    print("\n--- 相对原始参数的改动 ---")
    if trace:
        for k, old, new, note in trace:
            shown_old = old if not isinstance(old, str) or len(old) < 70 else old[:67] + "..."
            shown_new = new if not isinstance(new, str) or len(new) < 70 else str(new)[:67] + "..."
            print(f"  {k:34s} {shown_old!r:>30s} -> {shown_new!r}   ({note})")
    else:
        print("  （无）")

    dirs = out_dirs(args.out)
    seed_used = p.get("seed")
    base = f"{stem}_{seed_used}"
    orig_json = os.path.join(dirs["meta"], f"{stem}.original.json")
    with open(orig_json, "w", encoding="utf-8") as f:
        json.dump(orig, f, ensure_ascii=False, indent=2)
    req_path = unique_path(os.path.join(dirs["requests"], base + ".json"))
    with open(req_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n  seed = {seed_used}")
    print(f"  request  -> {req_path}")
    print(f"  original -> {orig_json}")

    if args.dry_run:
        print("\n[DRY-RUN] 未提交，未消耗额度。")
        return None

    print("\n提交中...")
    t0 = time.time()
    raw, ctype = submit(payload, args.token, args.proxy, trace=trace)
    png = unpack_png(raw)
    dt = time.time() - t0

    png_path = unique_path(os.path.join(dirs["images"], base + ".png"))
    with open(png_path, "wb") as f:
        f.write(png)
    print(f"  完成于 {dt:.1f}s -> {png_path}")

    got, got_how = extract_params(png, stem)
    print(f"  回执元数据来源: {got_how}")

    im_new = Image.open(io.BytesIO(png))
    print(f"  服务端 Source: {im_new.info.get('Source') or im_new.info.get('Software')}")

    # EXIF 里的 Comment 可能是 JSON 字符串，也可能是已解析的对象
    orig_comment = orig.get("Comment", orig) if isinstance(orig, dict) else orig
    if isinstance(orig_comment, str):
        orig_comment = json.loads(orig_comment)

    print(f"  原始 Source: {orig.get('Source', '<未知>') if isinstance(orig, dict) else '<未知>'}")
    print(f"  回执 model_name / model_hash: {got.get('model_name')} / {got.get('model_hash')}")

    rows, prompt_rows = compare(orig_comment, p, got)
    print("\n--- 参数核对（原始 / 发送 / 服务端回执）---")
    for k, o, s, g in rows:
        flag = "OK " if (s == g) else "!! "
        print(f"  {flag}{k:30s} 原={str(o)[:18]:18s} 发={str(s)[:18]:18s} 回={str(g)[:18]}")

    print("\n--- 提示词核对（原始 vs 回执）---")
    for k, o, g, verdict in prompt_rows:
        print(f"  [{verdict}] {k:28s} {str(o)[:36]!r} | {str(g)[:36]!r}")

    if args.compare_hash and orig_blob:
        try:
            oh = ahash(orig_blob)
            nh = ahash(png)
            d = hamming(oh, nh)
            if d is not None:
                print(f"\n感知哈希差异: {d}/{len(oh)} bits "
                      f"（跨模型版本仅供参考，越小越像）")
        except Exception as e:
            print("哈希比对跳过:", e)

    report = {
        "source": src,
        "metadata_from": how,
        "model": args.model,
        "original_source": orig.get("Source") if isinstance(orig, dict) else None,
        "returned_model_name": got.get("model_name"),
        "returned_model_hash": got.get("model_hash"),
        "overrides": [{"field": k, "from": str(o)[:200], "to": str(n)[:200], "note": nt}
                      for k, o, n, nt in trace],
        "check": [{"field": k, "original": str(o), "sent": str(s), "returned": str(g)}
                  for k, o, s, g in rows],
        "prompt_check": [{"field": k, "original": o, "returned": g, "verdict": v}
                         for k, o, g, v in prompt_rows],
        "output_png": png_path,
        "seed_used": seed_used,
        "request_json": req_path,
    }
    rep_path = os.path.join(dirs["meta"], base + ".report.json")
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告 -> {rep_path}")
    return png_path


def parse_size(s):
    a, b = s.lower().split("x")
    return int(a), int(b)


def default_token():
    """环境变量 NAI_TOKEN 优先，其次项目目录下的 token.txt。"""
    t = (os.environ.get("NAI_TOKEN") or "").strip()
    if t:
        return t
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    return None


def main():
    ap = argparse.ArgumentParser(description="NAI 作品复现工具")
    ap.add_argument("sources", nargs="*", help="图片 URL / 本地图片 / JSON 参数文件")
    ap.add_argument("--urls-file", default=None, help="每行一个 URL 的文本文件（# 开头为注释）")
    ap.add_argument("--model", default="nai-diffusion-5-full")
    ap.add_argument("--sampler", default="k_euler_ancestral", help="Euler a = k_euler_ancestral")
    ap.add_argument("--size", default="1280x768", type=parse_size)
    ap.add_argument("--keep-size", action="store_true",
                    help="沿用每张原图自身的尺寸，忽略 --size")
    ap.add_argument("--steps", type=int, default=23)
    ap.add_argument("--scale", type=float, default=7.0)
    ap.add_argument("--seed", default=None,
                    help="整数 seed；传 random 等价于 --random-seed；留空沿用原图 seed")
    ap.add_argument("--random-seed", action="store_true",
                    help="每张图各自随机一个 seed（不覆盖已有输出）")
    ap.add_argument("--noise-schedule", default=None, help="默认沿用原图（如 karras）")
    ap.add_argument("--prompt", default=None, help="替换基础提示词")
    ap.add_argument("--drop-characters", action="store_true", help="清空角色提示词")
    ap.add_argument("--keep-image-refs", action="store_true",
                    help="保留原图里的参考图 / img2img / 局部重绘字段"
                         "（默认会去掉，强制纯文生图）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    ap.add_argument("--proxy", default=os.environ.get("NAI_PROXY", DEFAULT_PROXY),
                    help="本地代理；不需要可传 --proxy ''")
    ap.add_argument("--referer", default=DEFAULT_REFERER,
                    help=f"抓图时使用的 Referer，缺省 {DEFAULT_REFERER}")
    ap.add_argument("--token", default=default_token())
    ap.add_argument("--dry-run", action="store_true", help="只构建请求并核对，不提交")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几个源")
    ap.add_argument("--sleep", type=float, default=3.0, help="批量提交间隔秒数")
    ap.add_argument("--compare-hash", action="store_true", help="输出与原图的感知哈希差异")
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("缺少 Token：设置环境变量 NAI_TOKEN 或传 --token")
    if args.proxy == "":
        args.proxy = None

    if args.seed is not None and str(args.seed).strip().lower() == "random":
        args.random_seed = True
        args.seed = None
    elif args.seed is not None and str(args.seed).strip() != "":
        args.seed = int(args.seed)
    else:
        args.seed = None
    if args.random_seed:
        args.seed = None

    if args.urls_file:
        with open(args.urls_file, encoding="utf-8") as f:
            args.sources += [ln.strip() for ln in f
                             if ln.strip() and not ln.lstrip().startswith("#")]
    if not args.sources:
        raise SystemExit("没有可处理的源：给出 URL/文件，或用 --urls-file 指定列表")

    srcs = args.sources[:args.limit] if args.limit else args.sources
    ok, fail = 0, 0
    for i, src in enumerate(srcs):
        try:
            process(args, src)
            ok += 1
        except SystemExit as e:
            print(f"  !! 跳过 {src}: {e}")
            fail += 1
        except Exception as e:
            print(f"  !! 出错 {src}: {e!r}")
            fail += 1
        if i < len(srcs) - 1 and not args.dry_run:
            time.sleep(args.sleep)

    print(f"\n{'=' * 74}\n完成：成功 {ok}，失败 {fail}\n{'=' * 74}")


if __name__ == "__main__":
    main()
