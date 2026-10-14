#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对比不同模型做同一份提示词分析的效果与速度。

    python bench_models.py qwen3:4b qwen3:8b orcarouter/Qwen3.8-27B-Uncensored:q4_K_M

评估维度：
  速度   —— 总耗时 / tok/s
  合规   —— 校验问题数、未归类标签数、是否截断
  质量   —— 几个可自动核对的点（见 check_quality）
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_analyze  # noqa: E402

# 用内置示例（32 个标签），输入完全一致，便于横向比
CASE = llm_analyze.DEMO


def check_quality(r):
    """几条能自动判定的小检查，再加人工看的提示。"""
    checks = {}
    cats = r.get("categories") or {}
    by_tag = {}
    for c, items in cats.items():
        for t in items:
            by_tag[t.lower()] = c

    # 1) 画师是否识别出来（示例里有 7 个画师名）
    artists = ["tresmarrow", "hth5k", "tudduls", "mustblove", "ppolar",
               "geulimykun_(skbyunea413)", "dikko"]
    checks["画师命中"] = sum(1 for a in artists if by_tag.get(a) == "画师")

    # 2) 负权重标签是否归到「抑制项」
    sup = ["multi generations", "multiple generations"]
    checks["抑制项命中"] = sum(1 for s in sup if by_tag.get(s) == "抑制项")

    # 3) 角色身份识别
    ch = (r.get("characters") or [{}])[0]
    ident = (ch.get("identity") or {})
    name = (ident.get("name") or "").lower()
    checks["认出德拉科"] = 1 if "draco" in name or "马尔福" in name else 0
    checks["认出性转"] = 1 if "genderswap" in json.dumps(ident, ensure_ascii=False).lower() \
        or "性转" in json.dumps(ident, ensure_ascii=False) else 0

    # 4) 说明性文字是否中文
    txt = (r.get("overview", {}).get("summary") or "") + \
          "".join(i.get("text", "") for i in (r.get("insights") or []))
    checks["中文达标"] = 1 if re.search(r"[\u4e00-\u9fff]", txt) else 0

    # 5) 拼写错误是否被指出
    alltext = json.dumps(r.get("insights") or [], ensure_ascii=False).lower()
    checks["挑出 earings"] = 1 if "earing" in alltext else 0
    return checks


def run(model, cfg_base):
    cfg = dict(cfg_base)
    cfg["model"] = model
    t0 = time.time()
    r = llm_analyze.analyze_prompts(CASE, cfg, use_cache=False, log=lambda *_: None)
    dt = time.time() - t0
    if not r.get("ok"):
        return {"model": model, "ok": False, "error": r.get("error"), "seconds": round(dt, 1)}
    st = r.get("stats") or {}
    return {
        "model": model,
        "ok": True,
        "seconds": round(dt, 1),
        "tok": st.get("eval_count"),
        "tok_s": st.get("tok_per_s"),
        "truncated": st.get("truncated"),
        "problems": len(r.get("problems") or []),
        "unassigned": len(r.get("unassigned") or []),
        "partial": r.get("partial"),
        "tags": r.get("tag_count"),
        "quality": check_quality(r),
        "title": (r.get("overview") or {}).get("title"),
        "summary": (r.get("overview") or {}).get("summary"),
        "insights": [i.get("text") for i in (r.get("insights") or [])],
        "cats": {k: len(v) for k, v in (r.get("categories") or {}).items()},
    }


def main():
    models = sys.argv[1:] or []
    if not models:
        print("用法: python bench_models.py <模型> [模型...]")
        return
    base = llm_analyze.load_config()
    out = []
    for m in models:
        print(f"跑 {m} …", flush=True)
        res = run(m, base)
        out.append(res)
        if res["ok"]:
            print(f"  {res['seconds']}s · {res['tok']} tok · {res['tok_s']} tok/s · "
                  f"问题 {res['problems']} · 未归类 {res['unassigned']}")
        else:
            print(f"  失败: {res['error']}")

    print("\n" + "=" * 96)
    print(f"{'模型':<42} {'秒':>6} {'tok/s':>7} {'输出':>6} {'问题':>5} {'未归类':>7}")
    print("-" * 96)
    for r in out:
        if not r["ok"]:
            print(f"{r['model']:<42} 失败: {str(r.get('error'))[:42]}")
            continue
        print(f"{r['model']:<42} {r['seconds']:>6} {str(r['tok_s']):>7} "
              f"{r['tok']:>6} {r['problems']:>5} {r['unassigned']:>7}")

    keys = ["画师命中", "抑制项命中", "认出德拉科", "认出性转", "中文达标", "挑出 earings"]
    print("\n质量核对（满分见括号）")
    print(f"{'模型':<42} " + " ".join(f"{k[:6]:>8}" for k in keys))
    print("-" * 96)
    for r in out:
        if not r["ok"]:
            continue
        q = r["quality"]
        print(f"{r['model']:<42} " + " ".join(f"{q.get(k, 0):>8}" for k in keys))
    print("满分：" + " ".join(f"{k[:6]}={v}" for k, v in
                             zip(keys, [7, 2, 1, 1, 1, 1])))

    print("\n人工看的输出样本")
    for r in out:
        if not r["ok"]:
            continue
        print(f"\n--- {r['model']} ---")
        print(f"  标题: {r['title']}")
        print(f"  概述: {str(r['summary'])[:150]}")
        for i in (r["insights"] or [])[:3]:
            print(f"  提示: {str(i)[:120]}")

    with open(os.path.join(llm_analyze.HERE, "bench_result.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n已写出 bench_result.json")


if __name__ == "__main__":
    main()
