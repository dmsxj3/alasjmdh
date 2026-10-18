# AGENTS.md — 本仓库的 AI 协作入口

> 本文件同时以 `MEMORY.md` 存在（内容完全一致）：`AGENTS.md` 供 DSH / ZCode / Codex 读取，`MEMORY.md` 供 WorkBuddy 读取。
> 修改任一份时，**必须同步修改另一份**，保持逐字节一致。

---

## 一、项目架构简述

本仓库是 [AzurLaneAutoScript](https://github.com/LmeSzinc/AzurLaneAutoScript) 的 Fork（`dmsxj3/alasjmdh`），由两条改造线叠加而成：

| 线 | 来源 | 内容 |
|---|---|---|
| **仪表盘** | [`Zuosizhu/Alas-with-Dashboard`](https://github.com/Zuosizhu/Alas-with-Dashboard)（本 Fork 历史里有其 212 个提交） | Oil / Coin / Gem / Pt / Cube 等资源记录与 Web 展示：`module/config/argument/dashboard.yaml`、`module/log_res/log_res.py`、`assets/gui/css/*`、`module/campaign/assets.py` 里的 `OCR_COIN_LIMIT` / `OCR_GEM` / `OCR_OIL_LIMIT` |
| **ModHandler**（自研） | 本 Fork | 悬浮窗倍率控制：`module/mod_handler/{mod_handler,mod_overlay,mod_prefs,mod_ui}.py` + `dev_tools/mod_*.py` 10 个工具 + 配置五源里的 `ModHandler` 组 |

**分支约定**（origin = `dmsxj3/alasjmdh`）：

| 分支 | 内容 | 同步方式 |
|---|---|---|
| `dev` | 仪表盘 + ModHandler + 上游同步（**主开发线**，也是**经验库的规范分支**） | 所有合并进这里 |
| `ziyong` | 内容**必须等于 dev** | 只做快进：`git push origin dev:ziyong`，**不要 merge** |
| `master` | 仪表盘 + 上游，**不含 ModHandler** | 本地平时不存在；需要时 `git switch -c master origin/master`，用完 `git branch -d master`。**同步必须用 `git worktree`，不要切主工作区 HEAD** |

> ⚠️ **本仓库没有 `main` 分支。** GitHub 默认分支是 `master`（`origin/HEAD -> origin/master`）；但**开发主线和经验库来源是 `dev`**。

**魔改面**：`git diff --name-only upstream/master dev` 当前输出 **54 个文件** —— 这就是本 Fork 相对上游的全部改动。其余文件可直接跟随上游。

---

## 二、任务路由（**强制**）

收到下列指令时，**必须先读取对应的 Skill 文件再动手**，不得凭记忆直接操作：

| 用户指令（同义表述均适用） | 必须先读取 |
|---|---|
| 「合并上游」「同步 Lme 更新」「合到 dev」「上游又更新了」「拉一下上游」 | **`.agents/skills/upstream-merger/SKILL.md`** |
| 「审查 dev 分支」「Code Review」「看看这次合并有没有问题」「检查有没有破坏本地魔改」「复核一下」 | **`.agents/skills/dev-reviewer/SKILL.md`** |

Skill 里定义了分阶段工作流（**从 GitHub 同步经验** → 分级处理 → 验证 → 写交接单 → 回写经验 → **推送回 GitHub**），**每一阶段都是必做项，不允许跳步**。

---

## 三、动态记忆账本

所有跨会话知识都落在 `.agents/memory/`，**它不是文档，是必须读写的工作账本**：

| 文件 | 作用 | 谁写 | 谁读 |
|---|---|---|---|
| `.agents/memory/fork-override-map.md` | 魔改保护清单：核心保护区 / 高危耦合区 / 纯上游区 + **GitHub 远端架构** | 合并 Agent、审查 Agent | 两者都读 |
| `.agents/memory/conflict-ledger.md` | 历史冲突判例库（按"文件 → 原因 → 解决范式"记录） | 合并 Agent | 合并 Agent |
| `.agents/memory/dev-audit-rules.md` | dev 分支审查红线（9 条，含检测命令与基线数字） | 审查 Agent | 审查 Agent |
| `.agents/memory/latest-sync-context.md` | 双 Agent 实时交接单（最近一次同步的上游接口变化 + 重点提示 + 远端状态） | 合并 Agent | 审查 Agent |

**规范源 = `origin/dev`**。`master` / `ziyong` 上的副本一律以 `origin/dev` 为准。

---

## 四、GitHub 远端实时同步（**铁律**）

因为这个项目托管在 GitHub 上，多个软件（WorkBuddy / DSH / ZCode / Codex）会在不同分支上读写同一套经验库。**经验不能只停留在本地**，必须形成闭环：

```
① 从 GitHub origin 拉取最新经验  →  ② 执行任务  →  ③ 修改经验后自动 push 回 GitHub
```

### 4.1 任务开始前（**第一件事**）

```bash
git -c http.version=HTTP/1.1 fetch origin --prune
git checkout origin/dev -- .agents/ AGENTS.md MEMORY.md
```

- 目的：确保读到的是 **GitHub 上最新**的经验，而不是本地可能过期的副本
- **不切分支**也能读 —— 在 `dev` / `master` / `ziyong` / 临时 worktree 里都通用
- 该命令会**覆盖本地未提交的同名改动**；若手上有未推送的账本修改，先 `git stash` 或 commit
- 首次执行时远端可能还没有 `.agents/`，报 `pathspec ... did not match` 属正常
- `fetch` 失败时**必须明确告知用户"经验库未能从 GitHub 同步"**，不得静默继续

### 4.2 任务结束后（**汇报之前的最后一件事**）

```bash
git add .agents/ AGENTS.md MEMORY.md
git commit -m "chore(agent-memory): sync latest merge/review experience"
git -c http.version=HTTP/1.1 push origin HEAD
```

- **必须用精确路径 `git add .agents/ AGENTS.md MEMORY.md`**，禁止 `git add -A` / `git add .` —— 仓库里有 **8 个用户明确要求不提交的未跟踪文件**（`dev_tools/gm_{capture,drive,ocr,s,scan}.py`、`dev_tools/q.py`、`启动ALAS-16384.bat`、`提交到dev.bat`）
- `git push origin HEAD` 推当前分支；若当前不在 `dev`，显式写 `git push origin HEAD:dev`
- 若代码提交（合并）还没推，**先推代码再推账本**
- 推送失败（网络）**不要静默吞掉**，必须告知用户并给出重试命令

---

## 五、强制回写规则（**不可协商**）

> **任何合并与审查任务，在向用户汇报结果之前，必须增量更新 `.agents/memory/` 下对应的账本，并推送到 GitHub。**

具体触发条件：

| 场景 | 必须写入 |
|---|---|
| 合并时遇到新的冲突模式（新文件 / 新类型如 modify/delete） | `conflict-ledger.md` 追加判例 |
| 新增了本地魔改文件，或保护区职责变化 | `fork-override-map.md` 更新分区 + 核对魔改面文件数 |
| 合并验证通过 | `latest-sync-context.md` **覆盖更新**（含上游关键接口变化） |
| 审查发现新的典型错误模式 | `dev-audit-rules.md` 追加红线 |
| 发现某条红线会误报 | 在对应红线里补"例外情况" |

**回写质量要求**：

- 必须具体到**真实文件路径 / 模块名 / 函数名 / 配置项**，并附**可执行的检测命令**
- 追加到各文件的「追加区」，**不要覆盖历史条目**
- 禁止"注意兼容性""小心冲突"这类空话

**汇报时必须说明三件事**：① 回写了哪个账本文件（或"本次无需回写"）② 经验提交的 SHA ③ 推送到了哪个远端分支。

---

## 六、GitHub 远端与推送纪律

| remote | URL | 用途 |
|---|---|---|
| `origin` | `https://github.com/dmsxj3/alasjmdh` | 我的 Fork，唯一推送目标 |
| `upstream` | `https://github.com/LmeSzinc/AzurLaneAutoScript` | 上游，只 fetch 不 push |

| 纪律 | 说明 |
|---|---|
| **pack 传输不稳** | 所有 `fetch` / `push` 必须加 `-c http.version=HTTP/1.1`，失败重试 2–3 次 |
| **只做 fast-forward** | `--dry-run` 输出**带 `+` 前缀 = 强推**，出现即中止排查。从未对本仓库强推过 |
| **`ziyong` 只快进** | `git branch -f ziyong dev && git push origin dev:ziyong` |
| **`master` 只走 worktree** | 绝不切主工作区 HEAD（否则 GUI 更新器误报并锁死） |
| **不提 PR** | 本 Fork 历史 PR 数 = **0**，所有改动直接 push 分支 |
| **没有 GitHub Actions** | `.github/workflows/` 不存在 → 推送不触发 CI，**所有验证必须推送前本地做完** |
| **代理** | 本仓库未配 git 代理，直连可用；`config/deploy.yaml` 里的 `GitProxy` 只作用于 ALAS 自己的更新器 |

---

## 七、本机环境速查

| 项 | 值 |
|---|---|
| 主开发克隆 | `C:\Users\zzz\workbuddy-ai\workbuddyai\AzurLaneAutoScript`（`dev`，WebuiPort **22277**，模拟器 `127.0.0.1:16384`，实例 `config/71测试.json`） |
| 实际跑 ALAS 的克隆 | `C:\Users\zzz\WorkBuddy\workbuddy\AzurLaneAutoScript`（`ziyong`，WebuiPort **22267**，模拟器 `127.0.0.1:16416`，实例 `config/自用.json`，`AutoUpdate: true`） |
| 仓库自带 python | `toolkit/python.exe`（3.7.6，含 yaml）—— 跑配置生成器/脚本用它 |
| 仓库自带 git | `toolkit/Git/mingw64/bin/git.exe`（2.28.0，merge 策略显示 `recursive`；系统 git 2.55 显示 `ort`） |
| `gh` CLI | 已安装 v2.100.0，认证为 `dmsxj3`（scope: repo / workflow / gist / read:org） |
| 不提交的 8 个文件 | `dev_tools/gm_{capture,drive,ocr,s,scan}.py`、`dev_tools/q.py`、`启动ALAS-16384.bat`、`提交到dev.bat`（后两个是 **GBK 编码**）。合并前后必须核对哈希 |

---

## 八、三条最容易踩的坑（摘要）

1. **`module/config/config_updater.py` 的 `min_depth=1`** —— 少了它，跑一次生成器就清空整组 Dashboard 配置。
2. **GUI 更新器会误报并锁死** —— `config/deploy.yaml` 是 `Branch: dev`，只要 HEAD 短暂离开 dev（哪怕几十秒），5 分钟一次的检查撞上就报"有新版可用"，且报过之后不会自愈。**所以 master 同步必须用 worktree。**
3. **`module/campaign/assets.py` 是头号冲突热点** —— 上游改 en/jp/tw 路径会撞上 Zuosizhu 的三个 Button，必须逐变量判定，详见 `conflict-ledger.md` C-001。
