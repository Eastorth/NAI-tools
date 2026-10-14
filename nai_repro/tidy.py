#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把散落的输出整理成「批次」结构。

处理两种旧布局：
  1) out/<作品名>/xxxx.png + request.json + original_params.json
  2) out/images/*.png + out/requests/*.json + out/meta/*.json

统一整理成：
  out/batches/<批次 id>/{batch.json, images/, requests/, meta/}

关键点：**按文件收集，不按作品名聚合**——同名作品可能有很多张不同 seed 的图。
seed 一律从成图元数据里读回，读不到才回退到文件名。

    python tidy.py            # 只预览，不动文件
    python tidy.py --apply    # 实际迁移（可重复执行，会并入同一批次）
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repro  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
BATCH_ROOT = os.path.join(OUT, "batches")
SKIP = {"images", "requests", "meta", "_src", "_thumbs", "batches"}

SEED_RE = re.compile(r"^(?P<stem>.+?)_(?P<seed>\d{1,10})$")


def split_stem(base):
    """'abc_p0_3630814026' -> ('abc_p0', 3630814026)

    先剥掉 .report/.original 标记与重名后缀（-2 / -3），
    否则上一轮生成的后缀会让解析失败、把每个文件当成一个作品。
    """
    base = re.sub(r"\.(report|original)$", "", base)
    prev = None
    while prev != base:          # 可能叠了 -2-2 这种多重后缀
        prev = base
        base = re.sub(r"-\d+$", "", base)
    m = SEED_RE.match(base)
    if m:
        return m.group("stem"), int(m.group("seed"))
    return base, None


def png_meta(p):
    try:
        with open(p, "rb") as f:
            d = repro.extract_params(f.read(), p)[0]
        c = d.get("Comment", d) if isinstance(d, dict) else d
        if isinstance(c, str):
            c = json.loads(c)
        return {
            "seed": c.get("seed"),
            "width": c.get("width"), "height": c.get("height"),
            "steps": c.get("steps"), "scale": c.get("scale"),
            "sampler": c.get("sampler"),
            "model_name": c.get("model_name"), "model_hash": c.get("model_hash"),
        }
    except Exception:
        return {}


def json_seed(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        pr = d.get("parameters", d)
        return pr.get("seed")
    except Exception:
        return None


def collect():
    """按文件收集。返回四个字典，键都是 (stem, seed) 或 stem。"""
    pngs, reqs, reps, origs = {}, {}, {}, {}

    # ---- 1) 旧的作品目录 ----
    for name in sorted(os.listdir(OUT)):
        d = os.path.join(OUT, name)
        if not os.path.isdir(d) or name in SKIP:
            continue
        files = {f.lower(): f for f in os.listdir(d)
                 if os.path.isfile(os.path.join(d, f))}
        first_seed = None
        for fn in sorted(files.values()):
            if not fn.lower().endswith(".png"):
                continue
            p = os.path.join(d, fn)
            seed = png_meta(p).get("seed")
            pngs[(name, seed)] = p
            if first_seed is None:
                first_seed = seed
        if "request.json" in files:
            p = os.path.join(d, files["request.json"])
            seed = json_seed(p)
            reqs[(name, seed if seed is not None else first_seed)] = p
        if "original_params.json" in files:
            origs[name] = os.path.join(d, files["original_params.json"])
        if "report.json" in files:
            p = os.path.join(d, files["report.json"])
            reps[(name, json_seed(p) or first_seed)] = p

    # ---- 2) 平铺的 images / requests / meta ----
    def scan(sub):
        d = os.path.join(OUT, sub)
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    for fn in scan("images"):
        if not fn.lower().endswith(".png"):
            continue
        p = os.path.join(OUT, "images", fn)
        stem, fseed = split_stem(os.path.splitext(fn)[0])
        pngs[(stem, png_meta(p).get("seed") or fseed)] = p

    for fn in scan("requests"):
        if not fn.lower().endswith(".json"):
            continue
        p = os.path.join(OUT, "requests", fn)
        stem, fseed = split_stem(os.path.splitext(fn)[0])
        reqs[(stem, json_seed(p) or fseed)] = p

    for fn in scan("meta"):
        if not fn.lower().endswith(".json"):
            continue
        p = os.path.join(OUT, "meta", fn)
        base = os.path.splitext(fn)[0]
        if ".original" in base:
            origs[base.split(".original")[0]] = p
        elif ".report" in base:
            stem, seed = split_stem(base.split(".report")[0])
            reps[(stem, seed)] = p
        else:
            stem, seed = split_stem(base)
            origs.setdefault(stem, p)

    return pngs, reqs, reps, origs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际迁移（默认只预览）")
    ap.add_argument("--name", default=None, help="批次名称")
    ap.add_argument("--resplit", action="store_true",
                    help="把现有批次按「作品 + 时段」重新切分（配合 --apply）")
    ap.add_argument("--gap", type=int, default=1800,
                    help="重新切分的时间间隔阈值（秒），默认 1800")
    a = ap.parse_args()

    if a.resplit:
        do_resplit(a.apply)
        return

    pngs, reqs, reps, origs = collect()
    keys = sorted(set(pngs) | set(reqs) | set(reps),
                  key=lambda t: (t[0], -1 if t[1] is None else t[1]))
    stems = sorted(set(origs) | {k[0] for k in keys})

    if not keys and not stems:
        print("没有需要整理的输出。")
        return

    # 用日期做 id，预览与执行一致；可重复执行，并入同一批次
    bid = time.strftime("%Y%m%d") + "_迁移"
    bdir = os.path.join(BATCH_ROOT, bid)
    name = a.name or f"迁移批次 {time.strftime('%Y-%m-%d')}"

    def rel(p):
        return os.path.relpath(p, bdir).replace("\\", "/")

    plan, items = [], []
    for stem in stems:
        if stem in origs:
            dst = os.path.join(bdir, "meta", f"{stem}.original.json")
            plan.append((origs[stem], dst))

    for stem, seed in keys:
        suffix = f"_{seed}" if seed is not None else ""
        entry = {"name": stem, "seed": seed, "status": "迁移", "source": "迁移"}
        if (stem, seed) in pngs:
            dst = os.path.join(bdir, "images", f"{stem}{suffix}.png")
            plan.append((pngs[(stem, seed)], dst))
            entry["png"] = rel(dst)
            entry.update({k: v for k, v in png_meta(pngs[(stem, seed)]).items()
                          if v is not None})
        if (stem, seed) in reqs:
            dst = os.path.join(bdir, "requests", f"{stem}{suffix}.json")
            plan.append((reqs[(stem, seed)], dst))
            entry["request"] = rel(dst)
        if stem in origs:
            entry["original"] = rel(os.path.join(bdir, "meta", f"{stem}.original.json"))
        if (stem, seed) in reps:
            dst = os.path.join(bdir, "meta", f"{stem}{suffix}.report.json")
            plan.append((reps[(stem, seed)], dst))
            entry["report"] = rel(dst)
        items.append(entry)

    print(f"目标批次: {bid}   （{name}）")
    print(f"作品图片 {len(pngs)} 张 · 请求 {len(reqs)} 个 · 报告 {len(reps)} 个 "
          f"· 原始参数 {len(origs)} 个")
    print(f"清单条目 {len(items)} 个，待迁移文件 {len(plan)} 个\n")
    for src, dst in plan[:16]:
        print(f"  {os.path.relpath(src, OUT)}")
        print(f"    -> {os.path.relpath(dst, OUT)}")
    if len(plan) > 16:
        print(f"  … 其余 {len(plan) - 16} 个同理")

    if not a.apply:
        print("\n（预览模式，加 --apply 才会真的迁移）")
        return

    for sub in ("images", "requests", "meta"):
        os.makedirs(os.path.join(bdir, sub), exist_ok=True)

    moved = 0
    for src, dst in plan:
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, repro.unique_path(dst))
        moved += 1

    # 并入已有清单（支持重复执行）
    mf = os.path.join(bdir, "batch.json")
    old = {}
    if os.path.isfile(mf):
        try:
            with open(mf, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}
    old_items = old.get("items") or []
    seen = {(i.get("name"), i.get("seed")) for i in old_items}
    merged = old_items + [i for i in items if (i.get("name"), i.get("seed")) not in seen]

    with open(mf, "w", encoding="utf-8") as f:
        json.dump({
            "id": bid,
            "name": old.get("name") or name,
            "created": old.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "settings": old.get("settings") or {},
            "note": "由 tidy.py 从旧布局迁移而来",
            "items": merged,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n已迁移 {moved} 个文件，批次现有 {len(merged)} 条记录 -> {bdir}")

    for nm in sorted(os.listdir(OUT)):
        d = os.path.join(OUT, nm)
        if os.path.isdir(d) and nm not in SKIP and not os.listdir(d):
            os.rmdir(d)
            print(f"  删除空目录: {nm}/")
    for sub in ("images", "requests", "meta"):
        d = os.path.join(OUT, sub)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
            print(f"  删除空目录: {sub}/")
        elif os.path.isdir(d):
            print(f"  !! {sub}/ 还剩 {len(os.listdir(d))} 个文件未迁移")


def work_of(stem):
    m = re.match(r"^(\d+)_p\d+$", stem)
    return m.group(1) if m else stem


def gather_batches():
    """扫描 out/batches/*，**按文件**汇总（同名同 seed 可能有多份，不能做字典键）。"""
    pngs, reqs, reps, origs, srcbatches = [], {}, {}, {}, set()
    if not os.path.isdir(BATCH_ROOT):
        return pngs, origs, srcbatches

    for b in sorted(os.listdir(BATCH_ROOT)):
        d = os.path.join(BATCH_ROOT, b)
        if not os.path.isdir(d):
            continue
        srcbatches.add(b)
        for sub in ("images", "requests", "meta"):
            sd = os.path.join(d, sub)
            if not os.path.isdir(sd):
                continue
            for fn in sorted(os.listdir(sd)):
                p = os.path.join(sd, fn)
                if not os.path.isfile(p):
                    continue
                base = os.path.splitext(fn)[0]
                if sub == "images" and fn.lower().endswith(".png"):
                    stem, seed = split_stem(base)
                    pngs.append({"stem": stem, "seed": seed, "path": p,
                                 "req": None, "report": None})
                elif sub == "requests" and fn.lower().endswith(".json"):
                    stem, seed = split_stem(base)
                    reqs.setdefault((stem, seed), []).append(p)
                elif ".original" in base:
                    origs.setdefault(base.split(".original")[0], p)
                elif ".report" in base:
                    stem, seed = split_stem(base.split(".report")[0])
                    reps.setdefault((stem, seed), []).append(p)

    # 把请求 / 报告逐一配对给图片（同键多份时按顺序各配一个）
    for r in pngs:
        k = (r["stem"], r["seed"])
        if reqs.get(k):
            r["req"] = reqs[k].pop(0)
        if reps.get(k):
            r["report"] = reps[k].pop(0)
    # 没配上的请求/报告也保留，作为独立条目
    for (stem, seed), lst in reqs.items():
        for p in lst:
            pngs.append({"stem": stem, "seed": seed, "path": None,
                         "req": p, "report": None})
    return pngs, origs, srcbatches


def _dest(bdir, sub, name, used):
    p = os.path.join(bdir, sub, name)
    if p in used or os.path.exists(p):
        root, ext = os.path.splitext(p)
        i = 2
        while f"{root}-{i}{ext}" in used or os.path.exists(f"{root}-{i}{ext}"):
            i += 1
        p = f"{root}-{i}{ext}"
    used.add(p)
    return p


def resplit(session_gap=1800):
    """把现有批次重新按「作品 + 时段」切分。"""
    recs, origs, srcbatches = gather_batches()
    rows = [r for r in recs if r["path"] or r["req"] or r["report"]]
    if not rows:
        print("没有可重新切分的图片。")
        return

    def mtime(r):
        return os.path.getmtime(r["path"] or r["req"] or r["report"])

    rows.sort(key=mtime)

    clusters, cur, last_t, last_w = [], [], None, None
    for r in rows:
        t = mtime(r)
        w = work_of(r["stem"])
        if last_t is not None and ((t - last_t) > session_gap or w != last_w):
            clusters.append(cur)
            cur = []
        cur.append(r)
        last_t, last_w = t, w
    if cur:
        clusters.append(cur)

    print(f"共 {len(recs)} 条记录（其中 {len([r for r in recs if r['path']])} 张有图），"
          f"切成 {len(clusters)} 个批次：\n")

    # 同一个作品名可能横跨多个批次，其 original 文件需要复制而不是移动
    from collections import Counter
    stem_clusters = Counter(r["stem"] for cl in clusters for r in cl)

    plan, out = [], []
    for cl in clusters:
        t0 = mtime(cl[0])
        work = work_of(cl[0]["stem"])
        bid = f"{work}_{time.strftime('%Y%m%d-%H%M%S', time.localtime(t0))}"
        bdir = os.path.join(BATCH_ROOT, bid)
        label = time.strftime("%m-%d %H:%M", time.localtime(t0))
        print(f"  {bid}   {work} · {label} 起 · {len(cl)} 张")
        used, items = set(), []
        for r in cl:
            stem, seed = r["stem"], r["seed"]
            suffix = f"_{seed}" if seed is not None else ""
            e = {"name": stem, "seed": seed, "status": "迁移", "source": "重新切分"}
            if r["path"]:
                dst = _dest(bdir, "images", f"{stem}{suffix}.png", used)
                plan.append((r["path"], dst, "move"))
                e["png"] = os.path.relpath(dst, bdir).replace("\\", "/")
                e.update({k: v for k, v in png_meta(r["path"]).items() if v is not None})
            if r["req"]:
                dst = _dest(bdir, "requests", f"{stem}{suffix}.json", used)
                plan.append((r["req"], dst, "move"))
                e["request"] = os.path.relpath(dst, bdir).replace("\\", "/")
            if stem in origs:
                dst = _dest(bdir, "meta", f"{stem}.original.json", used)
                plan.append((origs[stem], dst,
                             "copy" if stem_clusters[stem] > 1 else "move"))
                e["original"] = os.path.relpath(dst, bdir).replace("\\", "/")
            if r["report"]:
                dst = _dest(bdir, "meta", f"{stem}{suffix}.report.json", used)
                plan.append((r["report"], dst, "move"))
                e["report"] = os.path.relpath(dst, bdir).replace("\\", "/")
            items.append(e)
        out.append((bid, label, work, items))

    return plan, out, srcbatches


def do_resplit(apply_):
    r = resplit()
    if not r:
        return
    plan, out, srcbatches = r
    if len(out) > 60:
        print(f"\n!! 切出 {len(out)} 个批次，数量异常，已中止。"
              f"（多半是文件名解析失败，检查 split_stem）")
        return
    if not apply_:
        print("\n（预览模式，加 --apply 才会真的执行）")
        return

    for bid, label, work, items in out:
        bdir = os.path.join(BATCH_ROOT, bid)
        for sub in ("images", "requests", "meta"):
            os.makedirs(os.path.join(bdir, sub), exist_ok=True)
    moved, copied, skipped, errors = 0, 0, 0, []
    done_src = set()
    for src, dst, mode in plan:
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if not os.path.exists(src):
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            done_src.add(src)
            skipped += 1
            continue
        try:
            if mode == "copy":
                shutil.copy2(src, dst)
                copied += 1
            else:
                if src in done_src:
                    skipped += 1
                    continue
                shutil.move(src, dst)
                done_src.add(src)
                moved += 1
        except Exception as e:
            errors.append((src, repr(e)))
    for bid, label, work, items in out:
        bdir = os.path.join(BATCH_ROOT, bid)
        with open(os.path.join(bdir, "batch.json"), "w", encoding="utf-8") as f:
            json.dump({
                "id": bid, "name": f"{work} · {label}",
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "settings": {}, "note": "由 tidy.py --resplit 重新切分",
                "items": items,
            }, f, ensure_ascii=False, indent=2)

    # 清掉搬空的空壳批次，残留文件按作品名并入对应目标批次
    new_ids = {b for b, _, _, _ in out}

    def target_for(work):
        for bid, _label, w, _items in out:
            if w == work:
                return bid
        return out[-1][0] if out else None

    for b in sorted(srcbatches):
        if b in new_ids:
            continue
        d = os.path.join(BATCH_ROOT, b)
        if not os.path.isdir(d):
            continue
        imgs = [f for _r, _dd, fs in os.walk(d) for f in fs if f.lower().endswith(".png")]
        if imgs:
            print(f"  !! 批次 {b} 仍有 {len(imgs)} 张图未迁移，保留不动")
            continue
        rest = [os.path.join(r, f) for r, _dd, fs in os.walk(d)
                for f in fs if f != "batch.json"]
        strays = 0
        for p in rest:
            stem, _ = split_stem(os.path.splitext(os.path.basename(p))[0])
            tgt = target_for(work_of(stem)) or target_for(stem)
            if not tgt:
                continue
            sub = "meta" if (".original" in p or ".report" in p) else "requests"
            dst = _dest(os.path.join(BATCH_ROOT, tgt), sub,
                        os.path.basename(p), set())
            if not os.path.exists(dst):
                shutil.move(p, dst)
                strays += 1
        shutil.rmtree(d, ignore_errors=True)
        print(f"  清理空壳批次: {b}（并入 {strays} 个残留文件）")
    print(f"\n移动 {moved} · 复制 {copied} · 跳过 {skipped} · 生成 {len(out)} 个批次。")
    for s, e in errors[:10]:
        print(f"  !! {s}: {e}")


if __name__ == "__main__":
    main()
