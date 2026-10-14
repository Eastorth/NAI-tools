#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 urls.txt 里所有作品的原始参数，打印汇总表（不消耗额度）。"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repro

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.environ.get("NAI_PROXY", repro.DEFAULT_PROXY) or None

with open(os.path.join(HERE, "urls.txt"), encoding="utf-8") as f:
    urls = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]

print(f"{'#':>3} {'尺寸':>11} {'seed':>11} {'步':>4} {'CFG':>5} {'schedule':>9} "
      f"{'rescale':>7} {'角色':>4} {'模型':>8}  sampler")
print("-" * 108)

sizes, seeds, models = {}, {}, {}
for i, u in enumerate(urls):
    try:
        orig, how, name, blob = repro.load_source(u, PROXY, repro.DEFAULT_REFERER)
    except SystemExit as e:
        print(f"{i:>3}  !! {e}")
        continue

    c = orig.get("Comment", orig)
    if isinstance(c, str):
        c = json.loads(c)

    w, h = c.get("width"), c.get("height")
    src = str(orig.get("Source", "?"))
    model = src.replace("NovelAI Diffusion ", "")
    nchar = len((c.get("v4_prompt", {}).get("caption", {}) or {}).get("char_captions") or [])

    sizes[f"{w}x{h}"] = sizes.get(f"{w}x{h}", 0) + 1
    models[model] = models.get(model, 0) + 1

    print(f"{i:>3} {f'{w}x{h}':>11} {c.get('seed'):>11} {c.get('steps'):>4} "
          f"{c.get('scale'):>5} {str(c.get('noise_schedule')):>9} "
          f"{str(c.get('cfg_rescale')):>7} {nchar:>4} {model:>8}  {c.get('sampler')}")

print("-" * 108)
print("尺寸分布:", dict(sorted(sizes.items(), key=lambda kv: -kv[1])))
print("模型分布:", models)
