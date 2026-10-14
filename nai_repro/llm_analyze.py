#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词 LLM 分析模块
===================
本地小模型（Ollama）做**语义判断**，括号权重/字符位置等确定性部分交给 analyze.py。
两者合并成一份既结构化、又能逐字回填原文的分析结果。

单独用：
    python llm_analyze.py --demo            # 用内置例子跑一次
    python llm_analyze.py --json params.json
    python llm_analyze.py --check           # 只检查模型是否可用

配置优先级：llm_config.json > 环境变量 > 内置默认
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analyze  # noqa: E402

CONFIG_FILE = os.path.join(HERE, "llm_config.json")
CACHE_DIR = os.path.join(HERE, "out", "_llm")

DEFAULT_CONFIG = {
    "backend": "ollama",       # ollama | llamacpp
    "base_url": "http://127.0.0.1:8000",       # Ollama（OLLAMA_HOST=:8000）
    "model": "lukey03/qwen3.5-9b-abliterated-vision:latest",
    "num_ctx": 16384,
    "num_predict": 2400,
    "temperature": 0.1,
    "timeout": 900,
    "retries": 1,
    "keep_alive": "30m",       # 免得每次分析都重新加载模型（约 11 秒）
}

# 模式：full = 完整解读；fast = 只要分类与一句话概览
MODES = {
    "full": {
        "system": "SYSTEM",
        "keys": ("overview", "structure", "categories", "characters",
                 "reusable", "insights"),
        # 上限不是目标：规矩的模型写完就停，调大零成本；
        # 爱灌水的模型（qwen3-vl 系实测 full 要 2145 tok）需要足够余量
        "num_predict": 3200,
        "schema": True,
    },
    "fast": {
        "system": "SYSTEM_FAST",
        "keys": ("overview", "categories", "insights"),
        # 900 对爱灌水的模型不够：实测 qwen3-vl-4b 在 900 处被截断，
        # JSON 没闭合直接解析失败
        "num_predict": 1600,
        "schema": True,
    },
}

# 固定分类枚举 —— 模型不能自己造词
CATEGORIES = [
    "画师", "质量", "主体", "场景", "视角构图",
    "动作", "服装道具", "风格媒介", "抑制项", "其他",
]

LEVELS = ("info", "warn", "question")

# 硬错误前缀：出现这些才值得重试；漏几个编号之类的软问题直接接受
HARD_MARKS = ("顶层不是对象", "缺少顶层键", "categories 不是对象",
              "模型输出不是合法 JSON", "模型输出无法解析")

# 只分析这两个区块；负面提示词按需求不分析
ANALYZED_SLOTS = ("pos_base", "pos_chars")


# ---------------------------------------------------------------- 配置

def load_config(path=None):
    cfg = dict(DEFAULT_CONFIG)
    p = path or CONFIG_FILE
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
        except Exception as e:
            print(f"[warn] 读取 {p} 失败: {e}", file=sys.stderr)
    if os.environ.get("LLM_BASE_URL"):
        cfg["base_url"] = os.environ["LLM_BASE_URL"]
    if os.environ.get("LLM_MODEL"):
        cfg["model"] = os.environ["LLM_MODEL"]
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


# ---------------------------------------------------------------- 提示词模板

SYSTEM = """你是一个 NovelAI 提示词分析器。读取用户给出的提示词，输出**严格 JSON**。

关于标签编号：
用户会同时给出「原文」和一份**编号标签清单**。凡是引用标签的地方——
categories，以及 characters 里的 appearance / clothing / accessories /
expression / action——**一律只写编号**（整数数组），不要写标签文字。
这样不会出现拼写或引用错误，输出也短得多。

铁律：
1. 清单里的每个编号在 categories 中必须出现**恰好一次**，不能漏、不能重复。
   （characters 里可以重复引用，那是补充信息，不算数）
2. categories 的键只能用这些名字：%s
3. 所有说明性文字（title / summary / notes / insights）**必须用简体中文**，不要用英文。
4. 输出**紧凑 JSON**：不要换行、不要缩进、键值之间不要多余空格。
   直接输出一行，例如 {"overview":{"title":"...","summary":"..."},...}
5. 只输出 JSON，前后不要解释、不要 markdown 代码块。

分类口径：
- 画师：人名/署名，通常在开头
- 质量：masterpiece / best quality / highres 之类
- 主体：人物身份、数量、以及**外貌特征**（发型、体型、五官、表情、性格）
- 场景：地点、家具、环境物件
- 视角构图：front view / from below / cowboy shot / close-up 之类
- 动作：姿势与动作
- 服装道具：衣物、配饰、随身物
- 风格媒介：画风、渲染、光照、媒介
- 抑制项：被负权重压低的标签（原文里带 -N:: 的）
- 其他：确实归不进去的

篇幅限制（超了会被截断）：
- summary 最多 2 句
- structure.notes 最多 1 句；segments 按原文的换行与句点切分，role 用 4~8 个字
- reusable 最多 3 条，label 用中文，**用编号区间**：
    {"label":"画质组合","indices":[9,10,11,12],"note":""}
    indices 必须是**连续**的标签编号；代码会照原文逐字切出来，含权重括号
- insights 最多 3 条，每条 1 句
    info=背景知识，warn=值得动手处理的问题，question=需要人判断的疑点

输出结构：
{
  "overview": {"title":"", "summary":"", "genre":[], "mood":[], "scene":"", "style":[], "cast":[]},
  "structure": {"layout":"", "notes":"", "segments":[{"slot":"pos_base","index":0,"role":""}]},
  "categories": {%s},
  "characters": [{"index":1,"identity":{"name":"","fandom":"","variant":""},"appearance":[],"clothing":[],"accessories":[],"expression":[],"action":[]}],
  "reusable": [{"label":"","indices":[],"note":""}],
  "insights": [{"level":"info","text":""}]
}""" % ("、".join(CATEGORIES), ", ".join(f'"{c}":[]' for c in CATEGORIES))


SYSTEM_FAST = """你是一个 NovelAI 提示词分类器。读取用户给出的提示词与**编号标签清单**，
只做分类，输出**严格 JSON**。

铁律：
1. 清单里每个编号必须出现**恰好一次**，不能漏、不能重复。
   categories 里任何一个分类都**不允许为空对象式的应付**——必须真的把编号分完。
   第一个输出的键必须是 categories。
2. categories 的键只能用这些名字：%s
3. summary 用简体中文，最多 1 句（25 字以内）。
4. 输出**紧凑 JSON**：不要换行、不要缩进、键值之间不要多余空格。直接输出一行。
5. 只输出 JSON，前后不要解释、不要 markdown 代码块。

分类口径：
- 画师：人名/署名，通常在开头
- 质量：masterpiece / best quality / highres 之类
- 主体：人物身份、数量、外貌特征（发型、体型、五官、表情、性格）
- 场景：地点、家具、环境物件
- 视角构图：front view / from below / cowboy shot / close-up 之类
- 动作：姿势与动作
- 服装道具：衣物、配饰、随身物
- 风格媒介：画风、渲染、光照、媒介
- 抑制项：被负权重压低的标签（原文里带 -N:: 的）
- 其他：确实归不进去的

输出结构（不要输出任何其他字段）：
{
  "overview": {"title":"", "summary":"", "scene":""},
  "categories": {%s},
  "insights": [{"level":"warn","text":""}]
}
insights 最多 2 条，只写真正值得提醒的（拼写错误、重复标签、权重异常），
没有就留空数组。每条 1 句，简体中文。""" % (
    "、".join(CATEGORIES), ", ".join(f'"{c}":[]' for c in CATEGORIES))


def build_user_message(slots, tags):
    """同时给原文与编号标签清单；模型只需输出编号。"""
    by_slot = {}
    for tg in tags:
        by_slot.setdefault(tg["slot"], []).append(tg)

    parts = []
    for s in slots:
        parts.append(f"【原文 · {s['slot']}】\n{s['text']}\n")
        items = by_slot.get(s["slot"], [])
        if items:
            lines = [f"{tg['idx']}. {tg['text']}" for tg in items]
            parts.append(f"【{s['slot']} 标签清单】共 {len(items)} 个\n" + "\n".join(lines) + "\n")
    parts.append(f"清单共 {len(tags)} 个标签，请把 1~{len(tags)} 每个编号恰好归入一个分类。")
    return "\n".join(parts)


# ---------------------------------------------------------------- 调用

def _post(cfg, path, payload, timeout=None):
    url = cfg["base_url"] + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    # 本地服务，绝不能走系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout or cfg["timeout"]) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} @ {url} :: {body}") from None


def _get(cfg, path, timeout=15):
    req = urllib.request.Request(cfg["base_url"] + path)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- 模型探测

# 默认去哪几个本地端口找推理服务
PROBE_TARGETS = (
    ("ollama", "http://127.0.0.1:8000"),
    ("llamacpp", "http://127.0.0.1:8081"),
    ("ollama", "http://127.0.0.1:11434"),
    ("llamacpp", "http://127.0.0.1:8080"),
)


def _pretty(name):
    """'orcarouter/Qwen3.8-27B-Uncensored:q4_K_M' -> 'Qwen3.8-27B-Uncensored:q4_K_M'"""
    if not name:
        return ""
    n = name.split("/")[-1]
    return n


def _speed_hint(n_params):
    if not n_params:
        return ""
    b = n_params / 1e9
    if b <= 6:
        return "快"
    if b <= 15:
        return "中"
    return "慢"


def _probe(backend, base, timeout=4):
    """探测一个服务，返回该服务上可用的模型。连不上就返回空列表。"""
    cfg = {"base_url": base.rstrip("/"), "timeout": timeout}
    out = []
    try:
        if backend == "llamacpp":
            d = _get(cfg, "/v1/models", timeout=timeout)
            for m in (d.get("data") or []):
                meta = m.get("meta") or {}
                out.append({
                    "model": m.get("id"),
                    "n_params": meta.get("n_params"),
                    "quant": meta.get("ftype"),
                    "size": meta.get("size"),
                    "ctx": meta.get("n_ctx"),
                })
        else:
            d = _get(cfg, "/api/tags", timeout=timeout)
            for m in (d.get("models") or []):
                det = m.get("details") or {}
                ps = det.get("parameter_size") or ""
                try:
                    np_ = float(ps.rstrip("BbMm")) * (1e9 if "B" in ps else 1e6)
                except Exception:
                    np_ = None
                if det.get("families") and "embedding" in (m.get("capabilities") or []):
                    continue          # 嵌入模型不能做对话，跳过
                out.append({
                    "model": m.get("name"),
                    "n_params": np_,
                    "quant": det.get("quantization_level"),
                    "size": m.get("size"),
                    "ctx": det.get("context_length"),
                })
    except Exception:
        return []
    return out


def discover(extra_targets=(), timeout=4):
    """扫一遍本地端口，返回可直接选用的模型档案。"""
    profiles, seen = [], set()
    for backend, base in list(PROBE_TARGETS) + list(extra_targets):
        base = base.rstrip("/")
        for m in _probe(backend, base, timeout):
            mid = f"{backend}|{base}|{m['model']}"
            if mid in seen or not m.get("model"):
                continue
            seen.add(mid)
            bits = [("Ollama" if backend == "ollama" else "llama.cpp")
                    + " @" + base.rsplit(":", 1)[-1]]
            if m.get("n_params"):
                bits.append(f"{m['n_params']/1e9:.1f}B")
            if m.get("quant"):
                bits.append(str(m["quant"]))
            if m.get("size"):
                bits.append(f"{m['size']/1e9:.1f}GB")
            sp = _speed_hint(m.get("n_params"))
            if sp:
                bits.append(sp)
            profiles.append({
                "id": mid,
                "backend": backend,
                "base_url": base,
                "model": m["model"],
                "label": _pretty(m["model"]),
                "detail": " · ".join(bits),
                "speed": sp,
                "ready": True,
            })
    # 快的排前面
    order = {"快": 0, "中": 1, "慢": 2, "": 3}
    profiles.sort(key=lambda p: (order.get(p["speed"], 3), p["label"]))
    return profiles


def match_profile(cfg, profiles):
    """找出当前配置对应的档案 id。"""
    for p in profiles:
        if (p["backend"] == (cfg.get("backend") or "ollama")
                and p["base_url"].rstrip("/") == (cfg.get("base_url") or "").rstrip("/")
                and p["model"] == cfg.get("model")):
            return p["id"]
    return None


def check(cfg=None):
    """返回 (可用?, 说明)。"""
    cfg = cfg or load_config()
    backend = cfg.get("backend") or "ollama"
    try:
        if backend == "llamacpp":
            d = _get(cfg, "/v1/models")
            names = [m.get("id") for m in (d.get("data") or [])]
        else:
            d = _get(cfg, "/api/tags")
            names = [m.get("name") for m in (d.get("models") or [])]
    except Exception as e:
        return False, f"连不上 {cfg['base_url']}：{e}"
    if cfg["model"] not in names:
        return False, f"模型未加载。可用：{names}"
    return True, f"OK · {backend} · {cfg['model']} · {cfg['base_url']}"


def _chat_ollama(cfg, messages, num_predict, schema):
    body = _post(cfg, "/api/chat", {
        "model": cfg["model"],
        "stream": False,
        "think": False,
        "format": schema if schema else "json",
        "keep_alive": cfg.get("keep_alive") or "30m",
        "messages": messages,
        "options": {
            "num_ctx": cfg["num_ctx"],
            "num_predict": num_predict or cfg["num_predict"],
            "temperature": cfg["temperature"],
            "top_p": 0.9,
            "repeat_penalty": 1.05,
        },
    })
    txt = ((body.get("message") or {}).get("content") or "")
    ev = body.get("eval_count") or 0
    dur = body.get("eval_duration") or 0
    return txt, {
        "eval_count": ev,
        "prompt_tokens": body.get("prompt_eval_count") or 0,
        "done_reason": body.get("done_reason"),
        "truncated": body.get("done_reason") == "length",
        "tok_per_s": round(ev / (dur / 1e9), 1) if ev and dur else None,
        "load_s": round((body.get("load_duration") or 0) / 1e9, 1),
        "prompt_s": round((body.get("prompt_eval_duration") or 0) / 1e9, 1),
        "eval_s": round(dur / 1e9, 1),
        "total_s": round((body.get("total_duration") or 0) / 1e9, 1),
    }


def _chat_llamacpp(cfg, messages, num_predict, schema):
    """llama.cpp 的 OpenAI 兼容端点，结构化输出走 response_format。"""
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "max_tokens": num_predict or cfg["num_predict"],
        "temperature": cfg["temperature"],
        "top_p": 0.9,
        # 思考模型：关掉思维链，否则 content 会是空的、内容全跑到 reasoning_content
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "analysis", "strict": True, "schema": schema},
        }
    else:
        payload["response_format"] = {"type": "json_object"}

    body = _post(cfg, "/v1/chat/completions", payload)
    ch = (body.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    txt = msg.get("content") or ""
    if not txt.strip():
        # 兜底：模板不认 enable_thinking 时，从思维链里捞 JSON
        txt = msg.get("reasoning_content") or ""
    tm = body.get("timings") or {}
    usage = body.get("usage") or {}
    ev = usage.get("completion_tokens") or tm.get("predicted_n") or 0
    tps = tm.get("predicted_per_second")
    return txt, {
        "eval_count": ev,
        "prompt_tokens": usage.get("prompt_tokens") or tm.get("prompt_n") or 0,
        "done_reason": ch.get("finish_reason"),
        "truncated": ch.get("finish_reason") == "length",
        "tok_per_s": round(tps, 1) if tps else None,
        "load_s": 0.0,
        "prompt_s": round((tm.get("prompt_ms") or 0) / 1000, 1),
        "eval_s": round((tm.get("predicted_ms") or 0) / 1000, 1),
        "total_s": round(((tm.get("prompt_ms") or 0) + (tm.get("predicted_ms") or 0)) / 1000, 1),
    }


def chat(cfg, messages, num_predict=None, schema=None):
    """统一入口：按 backend 分发。返回 (文本, 统计)。"""
    if (cfg.get("backend") or "ollama") == "llamacpp":
        return _chat_llamacpp(cfg, messages, num_predict, schema)
    return _chat_ollama(cfg, messages, num_predict, schema)


def json_schema(mode="full"):
    """结构化输出 schema（Ollama 的 format 与 llama.cpp 的 response_format 通用）。

    用语法约束解码，模型**不可能**输出字符串冒充编号，也不可能漏掉必填键。
    """
    cat_props = {c: {"type": "array", "items": {"type": "integer"}}
                 for c in CATEGORIES}
    # 注意顺序：categories 放在最前，逼模型先完成分类再写散文。
    # 小模型很容易"偷懒"——先写一段摘要，然后把 categories 全填成空数组。
    props = {
        "categories": {"type": "object", "properties": cat_props,
                       "required": list(CATEGORIES)},
        "overview": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "scene": {"type": "string"},
                "genre": {"type": "array", "items": {"type": "string"}},
                "mood": {"type": "array", "items": {"type": "string"}},
                "style": {"type": "array", "items": {"type": "string"}},
                "cast": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "summary", "scene"],
        },
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": list(LEVELS)},
                    "text": {"type": "string"},
                },
                "required": ["level", "text"],
            },
        },
    }
    required = ["categories", "overview", "insights"]

    if mode == "full":
        props["structure"] = {
            "type": "object",
            "properties": {
                "layout": {"type": "string"},
                "notes": {"type": "string"},
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slot": {"type": "string"},
                            "index": {"type": "integer"},
                            "role": {"type": "string"},
                        },
                        "required": ["slot", "index", "role"],
                    },
                },
            },
            "required": ["layout", "notes", "segments"],
        }
        props["characters"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "identity": {
                        "type": "object",
                        "properties": {"name": {"type": "string"},
                                       "fandom": {"type": "string"},
                                       "variant": {"type": "string"}},
                        "required": ["name", "fandom", "variant"],
                    },
                    "appearance": {"type": "array", "items": {"type": "integer"}},
                    "clothing": {"type": "array", "items": {"type": "integer"}},
                    "accessories": {"type": "array", "items": {"type": "integer"}},
                    "expression": {"type": "array", "items": {"type": "integer"}},
                    "action": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["index", "identity", "appearance", "clothing",
                             "accessories", "expression", "action"],
            },
        }
        props["reusable"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "indices": {"type": "array", "items": {"type": "integer"}},
                    "note": {"type": "string"},
                },
                "required": ["label", "indices", "note"],
            },
        }
        required += ["structure", "characters", "reusable"]

    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------- 解析与校验

def extract_json(txt):
    """剥掉可能的 markdown 围栏 / 前后废话，取第一个完整 JSON 对象。"""
    if not txt:
        return None
    t = txt.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def validate(obj, n_tags, mode="full"):
    """校验模型输出。categories 的值应当恰好覆盖 1..n_tags 每个编号一次。"""
    problems = []
    if not isinstance(obj, dict):
        return ["顶层不是对象"]

    required = MODES.get(mode, MODES["full"])["keys"]
    for k in required:
        if k not in obj:
            problems.append(f"缺少顶层键 {k}")
    if problems:
        return problems

    cats = obj.get("categories")
    if not isinstance(cats, dict):
        problems.append("categories 不是对象")
        cats = {}

    seen, unknown_cat, bad_type = [], [], []
    for name, items in cats.items():
        if name not in CATEGORIES:
            unknown_cat.append(name)
        if not isinstance(items, list):
            bad_type.append(name)
            continue
        for v in items:
            if isinstance(v, int) and not isinstance(v, bool):
                seen.append(v)
            elif isinstance(v, str) and v.strip().isdigit():
                seen.append(int(v.strip()))
            else:
                bad_type.append(f"{name}:{v!r}")
    if unknown_cat:
        problems.append(f"出现了枚举外的分类：{unknown_cat}")
    if bad_type:
        problems.append(f"这些值不是编号：{bad_type[:8]}")

    want = set(range(1, n_tags + 1))
    got = set(seen)
    missing = sorted(want - got)
    extra = sorted(got - want)
    dup = sorted({v for v in seen if seen.count(v) > 1})
    if missing:
        problems.append(f"漏掉编号 {missing[:14]}（共 {len(missing)} 个）")
    if extra:
        problems.append(f"编号越界 {extra[:8]}")
    if dup:
        problems.append(f"编号重复 {dup[:8]}")
        # 重叠太严重说明模型压根没在做分类，得让用户看见
        if len(dup) > max(3, len(want) * 0.5):
            problems.append(
                f"分类不可信：{len(dup)}/{len(want)} 个编号被重复分到多个分类，"
                f"模型（{mode} 模式）没能给出有效分类，建议换更强的模型")

    for it in (obj.get("insights") or []):
        if isinstance(it, dict) and it.get("level") not in LEVELS:
            problems.append(f"insights.level 非法：{it.get('level')!r}")
            break
    return problems


# ---------------------------------------------------------------- 合并

def collect_source_tags(slots):
    """用 analyze.py 抽出所有标签（含权重、原文位置），并编上全局序号。"""
    out = []
    for s in slots:
        for tg in analyze.parse_tags(s["text"]):
            t = tg["text"].strip()
            if t:
                out.append({"idx": len(out) + 1, "slot": s["slot"], "text": t,
                            "weight": tg["weight"],
                            "rstart": tg.get("rstart"), "rend": tg.get("rend")})
    return out


def build_reusable(raw_items, source_tags, slots):
    """把模型给的编号区间还原成原文片段。

    只有编号**连续**时才按原文位置切片（这样能保留权重括号）；
    不连续就退化成逐标签拼接，避免把区间之间的无关内容也切进来。
    """
    by_idx = {tg["idx"]: tg for tg in source_tags}
    slot_text = {s["slot"]: s["text"] for s in slots}
    out = []
    for it in (raw_items or []):
        if not isinstance(it, dict):
            continue
        idxs = []
        for v in (it.get("indices") or []):
            n = v if isinstance(v, int) and not isinstance(v, bool) else (
                int(v) if isinstance(v, str) and v.strip().isdigit() else None)
            if n in by_idx and n not in idxs:
                idxs.append(n)
        if not idxs:
            continue
        idxs.sort()
        tags = [by_idx[i] for i in idxs]
        contiguous = idxs == list(range(idxs[0], idxs[-1] + 1))
        if (contiguous and len({t["slot"] for t in tags}) == 1
                and all(t.get("rstart") is not None for t in tags)):
            slot = tags[0]["slot"]
            text = slot_text[slot][tags[0]["rstart"]:tags[-1]["rend"]]
        else:
            text = ", ".join(t["text"] for t in tags)
        out.append({"label": str(it.get("label") or "片段"),
                    "indices": idxs, "text": text,
                    "contiguous": contiguous,
                    "note": str(it.get("note") or "")})
    return out


def resolve_refs(values, by_idx):
    """角色数组里模型可能写编号，统一换成标签文字。"""
    out = []
    for v in (values or []):
        n = None
        if isinstance(v, int) and not isinstance(v, bool):
            n = v
        elif isinstance(v, str) and v.strip().isdigit():
            n = int(v.strip())
        if n is not None and n in by_idx:
            out.append(by_idx[n]["text"])
        elif str(v).strip():
            out.append(str(v).strip())
    return out


def merge_elements(source_tags, categories):
    """把模型的编号分类贴回标签，产出 elements[] 与编号→分类映射。

    弱模型会把同一个编号塞进好几个分类（实测 qwen3-vl-4b 每个分类都灌
    39~59 个、几乎全重叠）。这时候「首次优先」会把垃圾全倒进第一个分类，
    比不分类还糟。所以：
        出现在 1 个分类  -> 采用
        出现在 2 个分类  -> 取先出现的那个（轻微歧义）
        出现在 3 个以上  -> 视为没表态，归入「其他」
    """
    hits = {}
    for cat, items in (categories or {}).items():
        if not isinstance(items, list):
            continue
        for v in items:
            n = v if isinstance(v, int) and not isinstance(v, bool) else (
                int(v.strip()) if isinstance(v, str) and v.strip().isdigit() else None)
            if n is not None:
                hits.setdefault(n, []).append(cat)

    idx_cat = {}
    for n, cats in hits.items():
        if len(cats) == 1:
            idx_cat[n] = cats[0]
        elif len(cats) == 2:
            idx_cat[n] = cats[0]
        # >=3 个分类：模型自相矛盾，不给分类

    elements, unassigned = [], []
    for tg in source_tags:
        cat = idx_cat.get(tg["idx"])
        if cat is None:
            cat = "其他"
            unassigned.append(tg["text"])
        elements.append({
            "id": f"e{tg['idx']}",
            "idx": tg["idx"],
            "slot": tg["slot"],
            "text": tg["text"],
            "weight": tg["weight"],
            "category": cat,
            "assigned": tg["idx"] in idx_cat,
        })

    # 同时给一份「分类 → 标签文字」的易读版本
    named = {}
    for e in elements:
        named.setdefault(e["category"], []).append(e["text"])
    return elements, named, unassigned


# ---------------------------------------------------------------- 主流程

# 结果后处理逻辑的版本。改动 validate / merge_elements / 兜底这些东西时
# 必须往上加，否则缓存里存的是按老规则算出来的结果，用户看不到新行为。
POSTPROC_VERSION = "2"


def cache_path(slots, cfg, mode):
    h = hashlib.sha1()
    for s in slots:
        h.update((s["slot"] + "\x00" + s["text"] + "\x00").encode("utf-8"))
    h.update((cfg["model"] + "\x00" + mode
              + "\x00v" + POSTPROC_VERSION).encode("utf-8"))
    return os.path.join(CACHE_DIR, h.hexdigest()[:20] + ".json")


def slots_from_prompts(prompts, include_chars=True):
    """从 prompts 结构里取出要分析的区块。"""
    slots = [{"slot": "pos_base", "text": prompts.get("pos_base") or ""}]
    if include_chars:
        for i, c in enumerate(prompts.get("pos_chars") or []):
            txt = (c.get("char_caption") if isinstance(c, dict) else str(c)) or ""
            if txt.strip():
                slots.append({"slot": f"pos_chars[{i}]", "text": txt,
                              "centers": (c.get("centers") if isinstance(c, dict) else None)})
    return [s for s in slots if s["text"].strip()]


def analyze_prompts(prompts, cfg=None, mode="full", include_chars=True,
                    use_cache=True, log=print):
    """核心入口：返回分析结果 dict（含 elements / categories / overview …）。"""
    cfg = cfg or load_config()
    mode = mode if mode in MODES else "full"
    spec = MODES[mode]
    slots = slots_from_prompts(prompts, include_chars)
    if not slots:
        return {"ok": False, "error": "没有可分析的提示词"}

    cp = cache_path(slots, cfg, mode)
    if use_cache and os.path.isfile(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                cached = json.load(f)
            cached["cached"] = True
            log("命中缓存")
            return cached
        except Exception:
            pass

    src_tags = collect_source_tags(slots)
    messages = [{"role": "system", "content": globals()[spec["system"]]},
                {"role": "user", "content": build_user_message(slots, src_tags)}]

    last_problems, obj, stats, txt = None, None, {}, ""
    # 预算是上限不是目标：模型写完就停，调大对规矩的模型零成本，
    # 但爱灌水的模型（实测 qwen3-vl 系会把几乎所有编号塞进每个分类）
    # 会把 JSON 撑爆到写不完。所以预算给足，且被截断时翻倍重试。
    budget = spec["num_predict"]
    cap = max(budget * 3, 6000)
    max_attempts = max(1 + cfg["retries"], 3)
    for attempt in range(max_attempts):
        t0 = time.time()
        log(f"调用模型（第 {attempt + 1} 次，预算 {budget} tok）…")
        try:
            txt, stats = chat(cfg, messages, num_predict=budget,
                              schema=json_schema(mode) if spec.get("schema") else None)
        except Exception as e:
            return {"ok": False, "error": f"模型调用失败：{e}"}
        truncated = bool(stats.get("truncated"))
        obj = extract_json(txt)
        if obj is None:
            problems = ["模型输出不是合法 JSON"]
        else:
            problems = validate(obj, len(src_tags), mode)
        if truncated:
            problems = list(problems) + [
                f"输出被 num_predict={budget} 截断，JSON 不完整"]
        last_problems = problems
        hard = [p for p in problems if p.startswith(HARD_MARKS)]
        if truncated:
            hard.append("truncated")     # 截断必须重试，且要给更大预算

        elapsed = time.time() - t0
        log(f"  {elapsed:.1f}s · {stats.get('eval_count')} tok · "
            f"{stats.get('tok_per_s')} tok/s"
            + ("" if not problems else f" · {problems[0][:80]}"))
        # 没有硬错误就接受（漏个别编号之类不值得再烧两分钟）
        if not hard or attempt == max_attempts - 1:
            break

        if truncated and budget < cap:
            # 重发同样的提示没用，得先把预算提上去
            budget = min(budget * 2, cap)
            log(f"  被截断，预算提到 {budget} tok 重试")
        else:
            messages = messages[:2] + [
                {"role": "assistant", "content": txt[:4000]},
                {"role": "user", "content":
                    "上面的输出有以下问题，请修正后重新输出完整 JSON：\n- "
                    + "\n- ".join(problems)},
            ]

    if obj is None:
        return {"ok": False, "error": "模型输出无法解析为 JSON",
                "problems": last_problems, "raw": (txt or "")[:2000]}

    elements, named_cats, unassigned = merge_elements(src_tags, obj.get("categories"))
    reusable = build_reusable(obj.get("reusable"), src_tags, slots)
    by_idx = {tg["idx"]: tg for tg in src_tags}
    chars = []
    for ch in (obj.get("characters") or []):
        if not isinstance(ch, dict):
            continue
        c2 = dict(ch)
        for f in ("appearance", "clothing", "accessories", "expression", "action"):
            c2[f] = resolve_refs(ch.get(f), by_idx)
        chars.append(c2)
    # 标题兜底：小模型经常把 title 写成空串
    # （实测 huihui qwen3-vl-abliterated 8b 恒为空），空标题在界面上很难看
    ov = dict(obj.get("overview") or {})
    if not str(ov.get("title") or "").strip():
        picks = [e["text"] for c in ("主体", "场景")
                 for e in elements if e["category"] == c][:3]
        if not picks:
            picks = [e["text"] for e in elements[:3]]
        if picks:
            ov["title"] = " · ".join(picks)[:48]
            ov["title_auto"] = True

    result = {
        "ok": True,
        "schema": "nai-prompt-analysis/1",
        "model": cfg["model"],
        "mode": mode,
        "slots": [s["slot"] for s in slots],
        "source_chars": sum(len(s["text"]) for s in slots),
        "tag_count": len(src_tags),
        "problems": last_problems or [],
        "partial": bool(last_problems),
        "unassigned": unassigned,
        "stats": stats,
        "overview": ov,
        "structure": obj.get("structure") or {},
        "categories": named_cats,
        "categories_by_index": obj.get("categories") or {},
        "elements": elements,
        "characters": chars,
        "reusable": reusable,
        "insights": obj.get("insights") or [],
        "cached": False,
    }
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  缓存写入失败: {e}")
    return result


# ---------------------------------------------------------------- CLI

DEMO = {
    "pos_base":
        "tresmarrow,hth5k, tudduls,mustblove,ppolar, 0.8::Geulimykun_(skbyunea413), dikko::\n"
        " Beautiful face, , masterpiece, best quality, newest, absurdres, highres, ,"
        "  -2::multi generations, multiple generations:: \n"
        "Bedroom, large bed, , very aesthetic, masterpiece, no text",
    "neg_base": "nsfw, lowres, ... （本模块不分析负面）",
    "pos_chars": [{
        "char_caption":
            "girl, draco malfoy, genderswap (mtf),   gold ring  earings,thick thighs,"
            " long hair, big breasts,, slytherin, , tsundere, black pantyhose,"
            " slender legs, , flustered",
        "centers": [{"x": 0.1, "y": 0.5}],
    }],
    "neg_chars": [],
}


def render(r):
    W = 78
    o = r.get("overview") or {}
    print("=" * W)
    print(f"{o.get('title', '(无标题)')}")
    print("=" * W)
    if o.get("summary"):
        print(o["summary"])
    for k, label in (("genre", "类型"), ("mood", "情绪"), ("style", "风格"), ("cast", "出场")):
        if o.get(k):
            print(f"  {label}: {' / '.join(map(str, o[k]))}")
    if o.get("scene"):
        print(f"  场景: {o['scene']}")

    st = r.get("structure") or {}
    if st:
        print(f"\n【结构】{st.get('layout', '')}")
        if st.get("notes"):
            print(f"  {st['notes']}")
        for s in (st.get("segments") or []):
            print(f"  段{s.get('index')} [{s.get('slot')}] {s.get('role', '')}"
                  f"  {s.get('note', '')}")

    print("\n【分类】")
    for c in CATEGORIES:
        items = (r.get("categories") or {}).get(c)
        if not items:
            continue
        print(f"  {c} ({len(items)}): " + ", ".join(map(str, items)))

    if r.get("characters"):
        print("\n【角色】")
        for ch in r["characters"]:
            ident = ch.get("identity") or {}
            who = " · ".join(x for x in (ident.get("name"), ident.get("fandom"),
                                         ident.get("variant")) if x)
            print(f"  角色 {ch.get('index')}  {who}")
            for f, label in (("appearance", "外貌"), ("clothing", "服装"),
                             ("accessories", "配饰"), ("expression", "神态"),
                             ("action", "动作")):
                if ch.get(f):
                    print(f"    {label}: {', '.join(map(str, ch[f]))}")

    if r.get("reusable"):
        print("\n【可复用】")
        for b in r["reusable"]:
            print(f"  [{b.get('label', '')}] {str(b.get('text', ''))[:90]}")

    if r.get("insights"):
        print("\n【提示】")
        mark = {"info": "·", "warn": "!", "question": "?"}
        for i in r["insights"]:
            print(f"  {mark.get(i.get('level'), '-')} {i.get('text', '')}")

    print(f"\n标签 {r.get('tag_count')} 个 · 未归类 {len(r.get('unassigned') or [])} 个"
          f" · {r.get('stats', {}).get('tok_per_s')} tok/s"
          f" · {r.get('stats', {}).get('eval_count')} tok"
          f" · 模式 {r.get('mode', 'full')}"
          f" · {'部分结果' if r.get('partial') else '校验通过'}")
    if r.get("problems"):
        for p in r["problems"]:
            print(f"  !! {p}")


def main():
    ap = argparse.ArgumentParser(description="提示词 LLM 分析")
    ap.add_argument("--check", action="store_true", help="只检查模型可用性")
    ap.add_argument("--demo", action="store_true", help="用内置示例跑一次")
    ap.add_argument("--json", help="从参数 JSON 文件读取（repro 的 original 格式）")
    ap.add_argument("--mode", default="full", choices=list(MODES),
                    help="full=完整解读；fast=只要分类与一句话概览")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", help="把结果写到指定 JSON")
    a = ap.parse_args()

    cfg = load_config()
    ok, msg = check(cfg)
    print(("模型可用 · " if ok else "模型不可用 · ") + msg)
    if a.check or not ok:
        return

    if a.json:
        with open(a.json, encoding="utf-8") as f:
            d = json.load(f)
        c = d.get("Comment", d)
        if isinstance(c, str):
            c = json.loads(c)
        vp = (c.get("v4_prompt") or {}).get("caption") or {}
        prompts = {"pos_base": vp.get("base_caption") or c.get("prompt") or "",
                   "pos_chars": vp.get("char_captions") or []}
    elif a.demo:
        prompts = DEMO
    else:
        ap.error("需要 --demo 或 --json")

    r = analyze_prompts(prompts, cfg, mode=a.mode, use_cache=not a.no_cache)
    if not r.get("ok"):
        print("失败:", r.get("error"))
        if r.get("raw"):
            print("原始输出:", r["raw"][:800])
        return
    render(r)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(f"\n已写出 {a.out}")


if __name__ == "__main__":
    main()
