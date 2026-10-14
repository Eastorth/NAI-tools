# NAI 作品复现工具

从 aitag 图床的 WebP 里提取 NovelAI 原始出图参数，改写后用指定模型/采样参数重新提交到
NovelAI 官方 API，并把结果与原始参数逐字段核对。

## 为什么能从 WebP 取到参数

图床把 NAI 的 PNG 转成了 WebP，但**把原始元数据保留在了 EXIF 里**：

| EXIF 标签 | 内容 |
|---|---|
| `0x010e` ImageDescription | 基础提示词 |
| `0x0131` Software | 出图模型，如 `NovelAI Diffusion V4.5 1229B44F` |
| `0x8769` → `0x9286` UserComment | **完整参数 JSON**（`Description` / `Comment` / `Source` …） |

所以不需要爬网页，直接读图就有出厂真值。

## 环境

- 本机 NovelAI 被 DNS 污染，必须走代理：默认 `NAI_PROXY` 或 `--proxy`，缺省 `http://127.0.0.1:7890`
- Token 读环境变量 `NAI_TOKEN`，或 `--token`；**脚本不会打印它**
- API 主机是 `https://image.novelai.net/ai/generate-image`（`api.novelai.net` 已废弃）
- 需要可用的订阅档位；实测账号为 Opus (tier 3)

## GUI（推荐）

双击 `start_gui.bat`，或：

```bash
python gui.py --open          # 默认 http://127.0.0.1:8770/
```

纯标准库 + Pillow，不需要 Flask / Node。界面左侧是数据源与参数，右侧是作品卡片。

- **解析参数**：批量抓图并提取原始参数（不消耗额度），自动生成缩略图
- **完整提示词**：每张卡片可展开查看基础提示词、每个角色提示词、负面基础与负面角色（全文，不截断）
- **批量替换**：查找/替换可作用于「基础 / 角色 / 负面」任一范围，支持正则与忽略大小写，
  先「预览」看命中数与涉及张数，再「应用」；可限定只作用于勾选的作品，也可一键还原
- **逐图编辑**：卡片上的「编辑这张」打开侧边编辑器，逐块修改提示词并保存
- **随机种子**：勾选后每张图各自随机一个 seed，配合不覆盖命名可以反复重跑对比
- **共有词高亮**：跨全部作品检测相同提示词，按共有程度用不同底色标记（见下节）
- **生成选中**：只跑勾选的作品，实时进度、日志、逐张状态
- **参数核对**：每张卡片展示「原始 / 发送 / 服务端回执」逐字段比对，不一致项自动展开
  卡片上会标出实际使用的 seed，与原图不同时标注 `← 已改`，并显示 request 落盘路径
- **模糊原图**：默认开启的隐私开关，鼠标悬停临时减淡
- Token 留空则用服务端 `NAI_TOKEN` 环境变量

编辑过提示词的作品会显示 `· 已修改` 标记，生成时会用编辑后的版本；
角色提示词的 `centers`（坐标）在编辑过程中原样保留。

服务端接口：`GET /api/status`、`GET /api/urlsfile`、`GET /api/prompts`、`GET /api/common`、`GET /img`；
`POST /api/settings`、`POST /api/parse`、`POST /api/run`、`POST /api/stop`、
`POST /api/prompts/set`、`POST /api/prompts/replace`、`POST /api/prompts/reset`、
`POST /api/analyze`。

## 共有词高亮

跨全部作品统计每个标签出现在多少张里，然后在**每张卡片的提示词里直接上色**：

| 底色 | 含义 | 门槛 |
|---|---|---|
| 🟩 绿色 | 全部共有 | 出现在**每一张**里 |
| 🟦 蓝色 | 多数共有 | ≥ 60% |
| 🟨 琥珀色 | 少数共有 | ≥ 2 张 |
| 无标记 | 独有 | 只有这一张 |

统计口径是**去重计数**：同一个词在一张图里出现多次也只记 1 张。

实测算例（23 张作品）：

```
全部共有  88 个
多数共有  39 个
少数共有  89 个
独有      66 个
```

侧栏「共有提示词」里可以：

- 开关高亮
- 「共有词表」查看分级清单（带出现次数与占比条）
- 一键复制全部共有词，直接拿去复用

标记是**逐字节可还原**的：高亮片段与间隙片段拼起来严格等于原文，
`[[[[cutesexyrobutts]]]]` 这类会被整体标色（含权重括号），
`1.5::` 这种组前缀不标色、组内标签标色。

### 批次：一天一个，凌晨 4 点换天

默认模式 `auto` 下，**同一天的生成全部追加进同一个批次**，不会一次生成就新建一个。

```
当天 03:59  ->  批次 20261013 (2026-10-13)
当天 04:00  ->  批次 20261014 (2026-10-14)
当天 23:59  ->  批次 20261014
次日 02:00  ->  批次 20261014     ← 4 点前仍算前一天
次日 05:00  ->  批次 20261015
```

实现上就是把时间戳往前推 4 小时再取日期：

```python
DAY_START_HOUR = 4
def day_anchor(t=None):
    return (time.time() if t is None else t) - DAY_START_HOUR * 3600
def day_key(t=None):
    return time.strftime("%Y%m%d", time.localtime(day_anchor(t)))
```

批次 ID 是 `day_20261013`，名字 `2026-10-13 每日批次`。
`ensure_day_batch()` 先查当天的批次在不在，**在就直接追加，不在才新建**，反复调用是幂等的。

侧栏批次下拉显示为「＋ 今天（2026-10-13）」，下面一行小字标明当前归属：

```
当前：每日批次（2026-10-13 04:00 起）· 已存在，直接追加
```

**手动新建独立批次的能力保留**：「新建批次」按钮仍按时间戳创建独立批次
（`20261014-012614_xxx`）并自动切到它，之后的生成追加进那个批次，不受每日规则影响。
在批次下拉里选「追加到：xxx」也能手动指定目标批次。

> ⚠️ 和资源库的联动：资源库里的「合并同作品的批次」会按作品号把批次并起来，
> 那会把每日批次拆散重组。既然批次现在按天分，这个按钮一般不该再点了。

## 提示词分析模块

提示词结构千变万化，固定规则跑不动，所以做了**两套**：一套确定性的本地解析，
一套可选的 LLM 语义分析。两者互补。

### analyze.py —— 确定性解析（免费、瞬时）

```bash
python analyze.py "<提示词>"
python analyze.py --text-file prompt.txt --json analysis.json
```

1. **权重语法**：`{{tag}}` ×1.1025、`[[[[tag]]]]` ×0.8227、`1.5::a, b::` 组内各 ×1.5、`-1:: tag::` 负权重
2. **结构分段**：按 `.` 切段，**保护 `1.5` 这类小数**，跳过 `::...::` 内部
3. **语义归类**：七个分类 + 未分类兜底，词典在 `categories.json`，可随时扩充
4. **按分类批量替换**：外科式替换，`[[[[cutesexyrobutts]]]]` 的权重括号原样保留

分词按 `,` `.` 与**换行**切分（换行也要算——很多作者用换行分段）。
所有片段拼接后能**逐字节还原原文**，界面高亮不会悄悄改掉提示词。

### llm_analyze.py —— LLM 语义分析（本地 27B，约 90~180 秒/张）

```bash
python llm_analyze.py --check          # 只检查模型可用性
python llm_analyze.py --demo           # 内置示例
python llm_analyze.py --json params.json
```

**分工原则**：括号权重、字符位置这些**确定性**的东西交给 `analyze.py`；
「这个标签到底算画师还是风格」这种**语义**判断交给模型。

**关键设计：模型只输出标签编号，不输出标签文字。**

用户消息里同时给「原文」和一份编号标签清单，模型只需回答
`{"画师": [1,2,3,4,5,6,7], "质量": [9,10,11,12], ...}`。这样：

- 杜绝逐字复制错误（模型抄错一个字母就会导致失配）
- 输出体积小一大截（103 个标签也只占几百 token）
- 校验变得很硬：1..N 每个编号必须出现恰好一次
- `reusable` 同理用**编号区间**，代码照原文切片，权重括号一并保留

**校验与容错**：

| 情况 | 处理 |
|---|---|
| JSON 解析失败 / 缺顶层键 | 硬错误，重试 1 次并把问题回灌给模型 |
| 漏几个编号 / 编号越界 | 软错误，**不重试**（省下两分钟），记进 `problems` 并标 `partial` |
| 角色数组里写了编号 | 代码自动换成标签文字 |
| 输出被 `num_predict` 截断 | 从 `done_reason` 检出并明确报告 |
| 模型不可用 | 明确报错，不影响其他功能 |

**缓存**：按「提示词内容 + 模型名」哈希存到 `out/_llm/`，原文没变就不重算。
界面上的「强制重跑」可以忽略缓存。

**配置**：`llm_config.json`（可被界面上填的服务地址/模型覆盖）。

```json
{
  "base_url": "http://127.0.0.1:8000",
  "model": "orcarouter/Qwen3.8-27B-Uncensored:q4_K_M",
  "num_ctx": 12288,
  "num_predict": 2400,
  "temperature": 0.1,
  "retries": 1
}
```

### 输出结构

```jsonc
{
  "mode": "full",              // full | fast
  "overview":  { "title", "summary", "genre", "mood", "scene", "style", "cast" },
  "structure": { "layout", "notes", "segments": [{"slot","index","role"}] },
  "categories": { "画师": ["tresmarrow", ...], "质量": [...], ... },   // 回填后的易读版
  "categories_by_index": { "画师": [1,2,3,4,5,6,7], ... },            // 模型原始输出
  "elements": [{ "id","idx","slot","text","weight","category","assigned" }],
  "characters": [{ "index","identity":{...},"appearance","clothing",
                   "accessories","expression","action" }],
  "reusable": [{ "label","indices","text","contiguous","note" }],
  "insights": [{ "level": "info|warn|question", "text" }],
  "problems": [], "partial": false, "unassigned": [], "stats": {...}
}
```

十个固定分类：`画师 · 质量 · 主体 · 场景 · 视角构图 · 动作 · 服装道具 · 风格媒介 · 抑制项 · 其他`。
模型不能自己造分类名，出现枚举外的会被校验拦住。

**按需求不分析负面提示词**，只分析 `pos_base` 与 `pos_chars`。

### 两种模式

| 模式 | 内容 | 27B 实测 | Spark 4B 实测 |
|---|---|---|---|
| `full` | 概览 + 结构 + 分类 + 角色细读 + 可复用 + 洞察 | 70 s | **4.6 s** |
| `fast` | 概览（一句话）+ 分类 + 最多 2 条提醒 | 33 s | **4.1 s** |

界面上一个下拉切换，批量时按模式估算耗时。

### 两套后端 / 两个模型

`llm_analyze.py` 支持两种后端，界面上切换：

| 后端 | 接口 | 模型 | 速度 | 分类完整度 |
|---|---|---|---|---|
| `ollama` | `/api/chat` | Qwen3.8-27B-Uncensored q4_K_M | 12.4 tok/s | **97% ~ 100%** |
| `llamacpp` | `/v1/chat/completions` | Huihui-Spark-X2.5-4B-abliterated Q4_K_M | **185 tok/s** | 73% ~ 81% |

同一份 103 标签的长提示词实测：

```
模型                      模式     耗时   tok/s   输出   未归类   完整度
Spark 4B (llama.cpp)     fast    4.1s   180.9    690      20      81%
Spark 4B (llama.cpp)     full    4.6s   187.0    849      28      73%
Qwen 27B (Ollama)        fast   79.8s     8.5    390       0     100%
Qwen 27B (Ollama)        full  129.7s     8.2    862       3      97%
```

**结论：速度能换到 17~28 倍，代价是约 20% 的标签归不进类**（落进「其他」）。
未归类的部分可以再用 `analyze.py` 的词典兜底，几乎是免费的。

⚠️ **两个模型同时加载会互相抢显存**（16GB 装不下 27B 的 18GB + 4B 的 2.6GB），
27B 会从 12.4 掉到 8.2 tok/s。建议用哪个起哪个。

#### llama.cpp 后端的来龙去脉

用户指定的 `huihui-ai/Huihui-Spark-X2.5-4B-abliterated` 是**自定义架构 `spark2_5`**：

- 上游只有 safetensors，没有 GGUF（但有第三方转好的：
  `okenk/Huihui-Spark-X2.5-4B-abliterated-GGUF`，`base_model` 正是 huihui 那个）
- **Ollama 0.32.15 不支持**（二进制里搜不到 `spark2_5`）
- **上游 llama.cpp 支持**（`llama-arch.cpp` 架构表里有）
- 本机 `E:\llama.cpp-0.2.0` 是旧源码树，也没有，且**没有任何编译工具链**

所以走的是**预编译二进制**：llama.cpp b11582 的 Windows CUDA 版
（`llama-b11582-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`），
解压在 `E:\llama_bin\cuda`，用 `start_llama_server.bat` 启动，监听 8081。

> 踩过的坑：`cudart` 压缩包里 dll 就在**根目录**、没有子文件夹，
> 我按子目录名过滤解压，结果一个都没解出来。
> 表现是 `ggml-cuda.dll` 静默加载失败、模型退回 CPU 跑 **22 tok/s**，
> 而日志里**没有任何 CUDA 相关记录**。补齐 3 个 dll
> （`cublas64_12.dll` / `cublasLt64_12.dll` / `cudart64_12.dll`）后变成 **191 tok/s**。

> 另一个坑：这是**思考模型**，内容默认跑进 `reasoning_content`、`content` 是空的。
> 需要传 `chat_template_kwargs: {"enable_thinking": false}`，并做一次兜底回退。

> 小模型还容易「偷懒」：语法约束能保证 JSON 结构合法，但保证不了它真的干活——
> 它可能输出 `{"categories":{"画师":[],...}}` 这样结构完美、内容全空的 JSON。
> 把 `categories` 在 schema 里**排到最前面**（逼它先分类再写散文）能显著改善。

### 性能优化记录（同一份 103 标签的长提示词）

| 阶段 | 耗时 | 输出 | 做法 |
|---|---|---|---|
| 起点 | 178 s | 2176 tok | 逐字引用标签、自由格式 JSON |
| +编号化 | 176 s | 2019 tok | 模型只输出编号（但 token 只降 7%） |
| +紧凑指令 | 72 s | 849 tok | 但质量崩了：漏 12 个编号，fast 模式把画师名写成字符串 |
| **+Schema 约束解码** | **70 s** | **852 tok** | 语法层面禁止错误，质量回来 |

关键在于**用 Ollama 的结构化输出（JSON Schema → GBNF 语法）**，
而不是靠提示词求模型「请输出紧凑 JSON」：

- 语法层面保证 JSON 合法、必填键齐全、`categories` 只能是整数数组
- 模型**不可能**再用字符串冒充编号（这是纯提示词方案堵不住的漏洞）
- 顺带把输出压紧 55%（2176 → 852 tok），因为语法不接受多余空白

> 首次使用某个 schema 时，llama.cpp 要编译一次 GBNF 语法，**第一枪会多花 1~2 分钟**。
> 之后同 schema 的调用都是正常的 70/33 秒。

### 实测性能

| 场景 | 标签数 | 模式 | 耗时 | 输出 |
|---|---|---|---|---|
| 短提示词（示例） | 32 | full | ~25 s | ~300 tok |
| 长提示词（双角色） | 103 | full | ~70 s | ~860 tok |
| 长提示词（双角色） | 103 | fast | ~33 s | ~400 tok |

模型：`orcarouter/Qwen3.8-27B-Uncensored:q4_K_M` @ Ollama `127.0.0.1:8000`，约 **12.4 tok/s**。

**速度的真实瓶颈是显存，不是参数量**：27B q4_K_M 有 18.0 GB，
而 RTX 5070 Ti 只有 16.3 GB——只有 76% 装进显存，剩下 4.3 GB 在内存里，
每个 token 都要走 PCIe。缩小 `num_ctx` 只能提速 11%（腾出的空间不够多塞一层）。

所以想再快就得换**能完整装进 16 GB 的模型**（8B 约 5.2 GB、4B 约 2.6 GB），
预计分别能到 ~50-70 tok/s 和 ~90-140 tok/s。
`bench_models.py` 已经写好，等模型到位可直接横向对比速度/合规/质量。

> 注意：`E:\llama.cpp-0.2.0\llama.cpp-0.2.0` 只是**源码仓库，没有编译产物**，
> 所以 `E:\llama_models\...Q8_0.gguf`（27GB）目前没有引擎能加载。
> 实际可用的是 Ollama 里已注册的 q4_K_M。
> `ollama pull` 实测卡死（registry 能连上但传输 0 字节），需要给 Ollama 配代理。

### 界面

侧栏「LLM 分析」**一个下拉切换模型**，不用再手填后端 / 地址 / 模型名：

```
模型  [刷新]
  ┌────────────────────────────────────────────┐
  │ huihui-spark-4b                  [快]  ▼   │
  └────────────────────────────────────────────┘
  llama.cpp @8081 · 4.1B · Q4_K - Medium · 2.6GB · 快
  · 当前使用

分析模式  [完整解读（概览+结构+角色+洞察） ▼]

▸ 手动指定服务        ← 折叠着，特殊情况下才用
```

**自动探测**：启动时和点「刷新」时会扫这几个端口——

| 后端 | 地址 |
|---|---|
| Ollama | `127.0.0.1:8000`、`127.0.0.1:11434` |
| llama.cpp | `127.0.0.1:8081`、`127.0.0.1:8080` |

当前配置不在探测目标里时，它自己的地址也会被加进去一起扫。
嵌入模型（`capabilities` 含 `embedding`）会被自动跳过。
选项按**速度档**排序（快 → 慢），并标注参数量、量化、体积。

选中即刻写回 `llm_config.json`，不需要再点「检查」或「应用」。
批量分析的耗时估算也改成按所选模型的速度档算，不再是硬编码数字。

其他：

- 卡片上单张「LLM 分析」按钮
- 「查看分析」打开完整面板：概览 / 结构 / 分类色块 / 角色细读 / 可复用片段（可一键复制）/ 提示
- 结果落进卡片，缓存命中会标注，`· 已修改` 之类标记同提示词编辑一致

### 权重语法对照

| 写法 | 含义 | 解析出的权重 |
|---|---|---|
| `{{tag}}` | 每层 ×1.05 | ×1.1025 |
| `[[tag]]` / `[[[[tag]]]]` | 每层 ÷1.05 | ×0.907 / ×0.8227 |
| `1.5::a, b::` | 整组 ×1.5 | 组内每个标签都是 ×1.5 |
| `-1:: tag::` | 负权重 | ×-1 |

### 界面上的「分析」按钮

每段提示词旁边的「分析」走的是 `analyze.py`（免费、瞬时），
显示分类色块、权重、分段与未分类清单。
`llm_analyze.py` 的结果则通过卡片上的「LLM 分析 / 查看分析」进入。

## 用法

```bash
export NAI_TOKEN='pst-...'

# 1) 只看参数、不烧额度
python repro.py --urls-file urls.txt --dry-run

# 2) 单张实跑（默认就是 V5 / Euler a / 1280x768 / 23 步 / CFG 7）
python repro.py <webp-url>

# 3) 批量（23 张）
python repro.py --urls-file urls.txt --limit 23 --sleep 3

# 4) 换提示词做链路验证
python repro.py <url> --prompt "..." --drop-characters

# 5) 输出与原图的感知哈希差异
python repro.py <url> --compare-hash
```

## 关键参数

| 选项 | 缺省 | 说明 |
|---|---|---|
| `--model` | `nai-diffusion-5-full` | 另一变体为 `nai-diffusion-5-curated`；V4.5 为 `nai-diffusion-4-5-full` |
| `--sampler` | `k_euler_ancestral` | Euler a |
| `--size` | `1280x768` | |
| `--keep-size` | 关 | 沿用每张原图自身尺寸（21 张是 1536x1536） |
| `--steps` | `23` | |
| `--scale` | `7.0` | 原图是 5.0 |
| `--seed` | 沿用原图 | 传整数指定，传 `random` 表示每张随机 |
| `--random-seed` | 关 | 每张图各自随机 seed（等价 `--seed random`） |
| `--noise-schedule` | 沿用原图（`karras`） | |

除上表外，**其余字段全部沿用原图**（`cfg_rescale=0.1`、`prefer_brownian=true`、
`deliberate_euler_ancestral_bug=false`、`sm/sm_dyn=false` 等）。回传前会剔除
`signed_hash` / `model_hash` / `model_name` / `request_type` / `version` /
`extra_passthrough_testing` 这些服务端生成字段。

## 默认强制纯文生图

**NovelAI 的元数据里从不包含"图像条件"的本体**——参考图、原图、蒙版都不写进去，
但配套的描述 / 提取度 / 强度数组会保留。原样回传的后果有两种：
被服务端以校验错误直接拒绝，或者悄悄产出一张与目标无关的图。

所以默认行为是把这些字段全部中性化：

| 字段 | 处理 | 来源 |
|---|---|---|
| `director_reference_descriptions` / `_information_extracted` / `_strengths` / `_secondary_strengths` | → `null` | Director 参考图 |
| `reference_information_extracted_multiple` / `reference_strength_multiple` | → `[]` | 参考图强度 |
| `img2img` / `add_original_image` / `strength` / `noise` / `extra_noise_seed` | → 中性 | img2img |
| `mask` / `image` | → `null` | 局部重绘 |
| `controlnet_model` / `controlnet_strength` | → `null` | ControlNet |
| `upscaled_enhance` / `upscale` / `pd_grid` / `crop_top` / `crop_left` / `original_width` / `original_height` | → `null` | Enhance / 放大（仅 img2img 可用） |
| `crash_hydra` / `hide_debug_overlay` | → `false` | 调试透传 |
| `request_type` | 剔除 | 原生局部重绘标记 |

只处理原参数里**确实存在**的字段，不会凭空添加；每次改动都会记进报告的 `overrides`。
生成后回执里的 `request_type` 应当是 `PromptGenerateRequest`，可据此确认服务端确实按纯文生图处理。

### 自愈：服务端拒绝字段时自动置空重试

清单不可能穷举。所以 `submit()` 还会**从报错里提取字段名**：

```
Validation error: upscaled_enhance is only supported on img2img requests
                 ^^^^^^^^^^^^^^^^^ 正则抓出来 → 置空 → 原样重试
```

形如 `Validation error: <字段名> ...` 的 400 会触发一次自动修正，
把该字段置为 `null` 后重试，并把它记进 `overrides`（标注「服务端校验拒绝，自动置空后重试」）。
每个字段最多自愈一次，不会打转。

> 真实案例：V5 自己产出的元数据会把 `upscaled_enhance` **漏到 `Comment` 顶层**
> （192 个源里出现 1 个）。作者用 NovelAI 的 Enhance 修过图，这个字段就留在元数据里了。
> 原样回传必然 400。现在会先被中性化清掉，万一还有漏网的自愈也能兜住。

> ⚠️ `straight_alpha` 和 `quality_boost` **不清理**——它们不是 img2img 专属，
> 服务端接受，且会实际影响出图（透明背景、质量增强），清掉反而破坏复现保真度。

想要原样保留，可以加 `--keep-image-refs`（CLI）或勾选界面上的
「保留参考图 / 局部重绘字段」。但除非你自己有办法补上参考图本体，否则保留只会换来 400。

解析完成后，界面上会给用过这些功能的作品加一个**黄色警示框**，
列出具体用了哪几项，避免你白跑一趟才发现。

**失败也会落盘**：请求被拒时会把请求体写成 `<名字>_<seed>.FAILED.json`，
放进该批次的 `requests/` 里，便于对着服务端报错排查。

## 输出

所有输出按**批次**管理：

```
out/
  batches/
    <作品号>_<时间戳>/          一个批次 = 一次生成任务
      batch.json               批次清单：名称、时间、参数快照、每条产出
      images/    <作品名>_<seed>.png
      requests/  <作品名>_<seed>.json
      meta/      <作品名>.original.json
                 <作品名>_<seed>.report.json
  _src/       抓下来的原图缓存（跨批次共用）
  _thumbs/    界面缩略图缓存
```

**同名文件永不覆盖**：已存在时自动追加 `-2`、`-3`。
配合 `--random-seed` 可以反复重跑而不丢上一次的结果。

### 批次怎么用

界面上方有一条批次栏：

- **下拉框**：`＋ 今天（2026-10-13）`（默认，走每日批次）或 `追加到：<某批次>`
- **新建批次**：手动建一个独立批次并命名，建完自动切过去
- **批次管理**：列出所有批次（名称、时间、张数、参数快照），每行可「图库 / 追加到 / 改名 / 目录 / 删除」

「图库」会以缩略图网格展示该批次的所有成图，点图在新标签页看原图。
删除批次会连同其中的图片和请求一起删掉，有二次确认。

`batch.json` 里的 `items` 记录了每张图的 seed、模型哈希、signed_hash、
尺寸/步数/CFG/采样器，以及 png / request / report 的相对路径——
所以批次是自包含的，直接拷走这个文件夹就能归档。

### 整理旧输出

```bash
python tidy.py                    # 预览：把散落的旧布局并入批次
python tidy.py --apply
python tidy.py --resplit          # 预览：按「作品 + 时段」重新切分
python tidy.py --resplit --apply
```

`--resplit` 用文件修改时间聚类（默认间隔阈值 1800 秒），能把历史上混在一起的输出
还原成当时跑的那几批。seed 一律从成图元数据读回，不靠文件名猜。
可重复执行，同一个作品的多份重名文件会带 `-2` 后缀共存。

CLI 也可以指定批次：`python repro.py <url> --batch "批次名"`。

## 实测结论

1. **V5 模型 ID** 为 `nai-diffusion-5-full`，`Source` 回执为 `NovelAI Diffusion V5 0ADF9AB7`。
2. **V5 固定 seed 是完全确定性的**：同参数连跑两次，像素 100% 一致（最大像素差 0）。
3. **`signed_hash` 对同一组参数稳定**：两次运行签名字节完全相同。PNG 文件哈希之所以会变，
   只是因为 `Generation_time` 被写进元数据。→ 可以用 `signed_hash` 作为严格的复现验收锚点。
4. V5 的元数据比 V4.5 多了 `model_name`、`model_hash`、`request_type`、`quality_boost`、
   `upscale`、`straight_alpha`、`tag_hint_*` 等字段；`request_type` 在普通文生图下是
   `PromptGenerateRequest`。
5. **V5 与 V4.5 的若干默认值不同**：V5 省略时 `deliberate_euler_ancestral_bug` 默认 `true`、
   `prefer_brownian` 默认 `false`、`noise_schedule` 默认 `native`（V4.5 分别是 `false`/`true`）。
   本工具沿用原图的显式值，所以不受影响。
6. 图床的 WebP 是**有损转码的派生图**（p0 是 1200x1200，而参数里是 1280x768），
   所以参数可信、但**像素不可作为比对基准**。
7. 原图的 `request_type` 是 `NativeInfillingRequest`，且字段里没有 mask / strength / 源图。
   如果它真是局部重绘产物，纯文生图无法逐像素还原——这点无法从参数单独证实，只能看画面有无接缝。

## 这 23 张作品的扫描结果

`python survey.py` 的输出：

| 项 | 结果 |
|---|---|
| 尺寸 | **21 张是 1536x1536**，只有 2 张（p0、p19）是 1280x768 |
| 模型构建 | **两个不同哈希**：`1229B44F`（p0、p19）与 `4BDE2A90`（其余 21 张） |
| 采样参数 | 全部一致：23 步 / CFG 5.0 / Euler a / karras / cfg_rescale 0.1 |
| 角色提示词 | 1 ~ 2 条，逐张不同 |
| seed | 每张不同；p0 与 p19 相同（`3630814026`） |

两点值得注意：

- **你说 1280x768，但 21 张原图其实是 1536x1536。** 统一按 1280x768 跑会改变构图与宽高比。
  想按原尺寸逐张跑就加 `--keep-size`。
- **同一批作品里出现了两个 V4.5 构建哈希**，说明作者创作期间 NovelAI 更新过后端。
  这正是"老图无法保证逐像素复现"的直接证据（见结论 2、3——同构建下 V5 是像素级确定的）。

## 安全

Token 曾以明文出现在对话里，**用完请到 NovelAI 账号设置里吊销并重新签发**。
本工具从环境变量读取，不落盘、不打印。
