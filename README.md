<div align="center">

# NAI Tools

**把 NovelAI 作品的原始元数据，变成能复现的图；再把图管起来。**

从作品 JSON / WebP EXIF / PNG 文本块里解析出完整参数，
在 NovelAI 上逐字段复现，并把服务端回执与原参数做交叉核对。
产出的成图由另一个独立服务负责浏览与整理。

</div>

---

## 两个项目

|  | 项目 | 作用 | 端口 |
|:--:|---|---|:--:|
| 🎨 | **[nai_repro](nai_repro/)** | NAI 复现工具 —— 解析元数据、复现出图、提示词分析 | `8770` |
| 🖼️ | **[nai_library](nai_library/)** | NAI 资源库 —— 浏览成图、查参数、整理批次 | `8780` |

两个**独立服务、独立进程、独立端口**，互不干扰。
资源库只读复现工具的产出目录，不参与出图。

```
        aitag.win / 本地文件 / 纯 JSON
                    │
                    │  作品原始元数据
                    ▼
      ┌───────────────────────────┐
      │        nai_repro          │
      │    NAI 复现工具  :8770    │
      │                           │
      │  解析 ─▶ 构造请求 ─▶ 提交 │
      │         ─▶ 核对回执       │
      └─────────────┬─────────────┘
                    │  写入 out/batches/<批次>/
                    │    images/  requests/  meta/
                    ▼
      ┌───────────────────────────┐
      │       nai_library         │
      │    NAI 资源库    :8780    │
      │                           │
      │  图墙 ─▶ 详情 ─▶ 批次管理 │
      └───────────────────────────┘
```

---

## 快速开始

### 环境

- **Python 3.8+**
- **Pillow** —— 唯一必需的第三方库

```bash
pip install Pillow
```

> `numpy` 是可选的，只用于感知哈希粗比对；没装会自动跳过，不影响任何主流程。
> 两个 Web 服务端都是纯标准库实现，没有 Flask / FastAPI 之类的依赖。

### 1 · 配置 NovelAI Token

在 `nai_repro/` 下新建 `token.txt`，把 Persistent Token 粘进去（只放这一行，别加引号）：

```
pst-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

也可以改用环境变量 `NAI_TOKEN`。

> Token 在 NovelAI 账户设置里生成。**这个文件已被 `.gitignore` 排除，不会进仓库。**

### 2 · 启动复现工具

```bash
cd nai_repro
python gui.py --port 8770
```

→ **http://127.0.0.1:8770/** 　（Windows 上也可直接双击 `start_gui.bat`）

### 3 · 启动资源库

```bash
cd nai_library
cp lib_config.example.json lib_config.json   # 改 nai_dir 指向你的 nai_repro
python lib_server.py --open
```

→ **http://127.0.0.1:8780/** 　（Windows 上也可双击 `start_library.bat`）

### 4 ·（可选）启用提示词分析

分析模块支持两套本地推理后端，界面上下拉一键切换——会自动扫描本地端口列出可用模型，
不用手填地址和模型名。

| 后端 | 适合 | 启动方式 |
|---|---|---|
| **Ollama** | 质量优先 | `ollama pull <模型>`，默认监听 `8000` |
| **llama.cpp** | 速度优先 | 运行 `nai_repro/start_llama_server.bat`，监听 `8081` |

llama.cpp 后端存在的意义：**有些自定义架构 Ollama 不认**（例如 `spark2_5`），
只能走 llama.cpp 的预编译二进制。

---

## 🎨 nai_repro · NAI 复现工具

### 读得进

- 三种来源：**图片 URL** / **本地图片** / **纯 JSON**
- WebP 走 EXIF `UserComment`，PNG 走 `Comment` 文本块（`tEXt`），两种格式都能解析
- 完整还原 `v4_prompt`：基础提示词、**角色分区与坐标**、负面提示词
- 权重语法原样保留，不做任何"美化"：

| 写法 | 含义 |
|---|---|
| `{tag}` | ×1.05（每层） |
| `[[tag]]` | ÷1.05（每层） |
| `N::a, b::` | 组权重 |
| `-N::tag::` | 负权重 |

### 复现得准

- 提交后把回执与原始参数**逐字段核对**：`原值 / 发送值 / 服务端返回值` 三列对照
- 参数一致时输出是**像素级一致**的——同参数实测 `max diff = 0`，`signed_hash` 相同，
  文件哈希不同仅因为元数据里的 `Generation_time`
- 默认**强制纯文生图**：自动中性化 Director 参考图、img2img、局部重绘、ControlNet 等
  图像条件字段，避免 43/154 那种参考图长度不匹配的 400
- 服务端因某字段拒绝时**自动置空并重试**——从
  `Validation error: <字段名> ...` 里正则提取字段名，置 `null` 后原样重发

### 用得顺

- **批次按天分，凌晨 4 点换天**：同一天的生成全部进同一个批次，不会一次生成就建一个。
  手动新建独立批次的能力保留
- **跨图共有提示词高亮**：所有图里都出现的词自动标醒目底色
- **批量替换**：按分类范围做正则或字面替换
- **参数核对报告**：每个批次里存 `meta/*.report.json`

### 提示词分析

两层，各司其职：

| 模块 | 性质 | 耗时 |
|---|---|---|
| `analyze.py` | 确定性解析：分类、权重括号、共有词 | **瞬时，免费** |
| `llm_analyze.py` | LLM 语义分析：概览、结构、角色细读、可复用片段、洞察 | 4 ~ 130 秒 |

输出结构由 **JSON Schema 约束解码**保证——模型不可能输出字符串冒充编号，也不可能漏掉必填键。
模型只负责给出**标签编号**，代码负责还原原文（含权重括号）。

实测对比（同一份 103 标签的长提示词）：

| 模型 | 模式 | 耗时 | 速度 | 分类完整度 |
|---|---|--:|--:|--:|
| Huihui-Spark-X2.5-4B @ llama.cpp | full | **4.6 s** | 185 tok/s | 73% |
| Huihui-Spark-X2.5-4B @ llama.cpp | fast | **4.1 s** | 181 tok/s | 81% |
| Qwen3.8-27B @ Ollama | full | 129.7 s | 8.2 tok/s | **97%** |
| Qwen3.8-27B @ Ollama | fast | 79.8 s | 8.5 tok/s | **100%** |

> 速度能换到 17~28 倍，代价是约 20% 的标签归不进类。日常用 4B，重要作品切 27B。

---

## 🖼️ nai_library · NAI 资源库

- **瀑布流图墙** —— 按原始比例完整显示，**绝不裁切**；滚动到底自动加载下一批
  （JS 真瀑布流，按最短列投放；服务端返回 PNG 头里的真实宽高，图片没加载也算得准高度，
  所以布局不会跳）
- **右侧详情三标签页**

  | 标签 | 内容 |
  |---|---|
  | 信息 | 参数、来源模型、条件检测警告、参数核对、文件路径 |
  | 提示词 | 基础 / 各角色 / 负面，每段可单独复制；角色带坐标 |
  | LLM 分析 | 复现工具跑过的分析，在这里直接看完整报告 |

- **全屏灯箱** —— 实际尺寸切换、`←` `→` 翻页、`Esc` 关闭
- **批次管理** —— 改名、按作品号重命名、合并同作品的批次
- **导出选中** —— 把勾选的图复制到一个扁平目录，方便直接拿出去用

---

## 目录结构

```
.
├── .gitignore
├── README.md
│
├── nai_repro/                   🎨 NAI 复现工具
│   ├── repro.py                 CLI 核心：解析 / 构造请求 / 提交 / 核对
│   ├── gui.py                   本地 Web 界面
│   ├── ui.html                  前端单页（无框架）
│   ├── analyze.py               确定性提示词解析
│   ├── llm_analyze.py           LLM 语义分析（Ollama + llama.cpp 双后端）
│   ├── tidy.py                  旧输出迁移与重排
│   ├── fetch.py                 断点续传下载器（走本地代理）
│   ├── survey.py                批量扫描元数据
│   ├── bench_models.py          跨模型速度 / 合规 / 质量对比
│   ├── determinism.py           确定性验证
│   ├── categories.json          分类定义
│   ├── llm_config.example.json
│   ├── start_gui.bat
│   └── start_llama_server.bat
│
└── nai_library/                 🖼️ NAI 资源库
    ├── lib_server.py            服务端（只读 nai_repro 的 out/）
    ├── lib.html                 前端单页（无框架）
    ├── lib_config.example.json
    └── start_library.bat
```

复现工具产出的批次数据（**不入库**）：

```
nai_repro/out/batches/<批次>/
├── batch.json          批次清单：每张图的 seed / 模型哈希 / signed_hash / 文件相对路径
├── images/             成图 PNG（含完整 NAI 元数据）
├── requests/           实际发出的请求 JSON
└── meta/               参数核对报告、原始参数快照
```

批次是**自包含**的——直接拷走这个目录就能归档。

---

## 关于本仓库

**仓库里只有代码，不含任何成图与凭据。** 排除规则见 [`.gitignore`](.gitignore)，
被挡在外面的约 **518 MB**：

| 排除内容 | 体积 | 说明 |
|---|--:|---|
| `nai_repro/out/` | 507.9 MB | 批次数据：成图、请求、报告 |
| `nai_repro/samples/` | 115 KB | 测试用成图样本 |
| `nai_library/cache/` | 10.4 MB | 缩略图缓存，可随时重建 |
| `__pycache__/` | 0.2 MB | Python 字节码 |
| `token.txt` | —— | NovelAI 密钥 |
| 本机配置文件 | —— | `llm_config.json` / `lib_config.json`（含本机路径），仓库里放 `.example.json` 模板 |

### 网络

- **NovelAI API** 与 **GitHub** 在本机均**无法直连**，需要代理
  （实测直连 github.com 21 秒超时，走 `127.0.0.1:7890` 正常）。
  `fetch.py` 通过环境变量 `FETCH_PROXY` 指定代理。
- 仓库已配置 `http.https://github.com.proxy`，提交代码时不用另设。

### 环境变量

| 变量 | 用途 |
|---|---|
| `NAI_TOKEN` | NovelAI Persistent Token（替代 `token.txt`） |
| `NAI_DIR` | 覆盖资源库的 `nai_dir` 配置 |
| `FETCH_PROXY` | `fetch.py` 使用的代理地址 |
| `OLLAMA_HOST` | Ollama 监听地址 |

---

<div align="center">
<sub>仅供个人学习与自有作品归档使用。请遵守 NovelAI 服务条款与当地法律法规。</sub>
</div>
