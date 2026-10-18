---
name: dev-reviewer
description: 审查 AzurLaneAutoScript Fork 的 dev 分支改动（合并后复核、日常 Code Review）。当用户说「审查 dev 分支」「Code Review」「看看这次合并有没有问题」「检查有没有破坏本地魔改」时使用。强制要求先读审查红线与合并 Agent 留下的交接单，审查结束前必须回写经验。
---

# dev 分支审查 Agent 规范（dev-reviewer）

## 适用仓库

`C:\Users\zzz\workbuddy-ai\workbuddyai\AzurLaneAutoScript`（origin = `dmsxj3/alasjmdh`，upstream = `LmeSzinc/AzurLaneAutoScript`）

---

## 阶段 1：读取上下文（**开始审查之前**）

### 1.0 ★ 铁律：先从 GitHub 远端拉取最新经验（**第一件事，不可跳过**）

经验库以 **`origin/dev`** 为唯一规范源。审查所依据的红线与交接单，可能刚被合并 Agent（或在别的软件/分支上）更新过 —— **必须先同步**，否则你会用旧红线审查新代码。

```bash
d="C:/Users/zzz/workbuddy-ai/workbuddyai/AzurLaneAutoScript"

# 1) 拉远端（pack 传输不稳，必须带 HTTP/1.1，失败重试 2-3 次）
git -C "$d" -c http.version=HTTP/1.1 fetch origin --prune

# 2) 用 GitHub 上的最新经验库覆盖本地（不切分支）
git -C "$d" checkout origin/dev -- .agents/ AGENTS.md MEMORY.md
```

**注意事项**：

- 该 `checkout` 会**覆盖本地未提交的同名改动**。若本地账本有未推送修改，先 `git stash` 或 commit。
- 本仓库**没有 `main` 分支**；规范源是 `origin/dev`。
- 若 `fetch` 失败，**必须明确告知用户"经验库未能从 GitHub 同步，本次审查基于本地版本"**，并在报告里降低结论置信度。

### 1.1 读取账本

必须按顺序读取（读取的是刚同步下来的最新版本）：

1. `.agents/memory/dev-audit-rules.md` —— 9 条红线，是本次审查的检查表
2. `.agents/memory/fork-override-map.md` —— 哪些文件属于保护区，被改了就是问题；**特别看第五节「GitHub 远端架构与交互经验」**
3. `.agents/memory/latest-sync-context.md` —— **合并 Agent 留下的交接单**，里面有本次上游改了哪些关键接口、以及它点名要你重点看的地方

然后确定审查范围：

```bash
git rev-parse --abbrev-ref HEAD                 # 应为 dev
git log --oneline -5
git diff --name-only <基线> HEAD                # 基线通常是上次审查点或 backup-dev-<日期>
git -c core.quotepath=false status --porcelain
```

> 若 `latest-sync-context.md` 缺失或明显过期（时间戳与最近合并提交不符），**先报告这个事实**，再继续审查 —— 因为上游接口变化信息缺失会让交叉审查失效。

---

## 阶段 2：交叉审查

### 2.1 检查是否破坏本地魔改特性

对照 `fork-override-map.md` 的核心保护区逐项验证：

| 检查点 | 判据 |
|---|---|
| ModHandler 四件套 | `module/mod_handler/{mod_handler,mod_overlay,mod_prefs,mod_ui}.py` 都在，且能 `py_compile` |
| `min_depth=1` | `grep -n "min_depth=1" module/config/config_updater.py` 必须命中 |
| `dashboard.yaml` | `git cat-file -e HEAD:module/config/argument/dashboard.yaml`，且 `Dashboard:` 的值仍是 list |
| ModHandler 工具链 | `ls dev_tools/ \| grep '^mod_'` 应为 10 个 |
| ModHandler 任务注册 | `grep -n "ModHandler" module/config/argument/task.yaml` |
| 仪表盘 CSS | `assets/gui/css/` 5 个文件都在，且未被上游同名文件整体覆盖（比对 dashboard 相关选择器是否还在） |
| fork 独有资产 | `assets/cn/campaign/{OCR_COIN_LIMIT,OCR_GEM,OCR_OIL_LIMIT}.png` 存在且被 `module/campaign/assets.py` 引用 |

### 2.2 检查是否与最近一次上游更新产生逻辑冲突

结合 `latest-sync-context.md` 的"上游关键变动"，重点看**新增接触点**：

- 上游新增了资产常量（如 `PAUSE_Bullet`、`ISLAND_RESTAURANT_ANIMATION_CHECK`）→ 确认所有消费方都纳入了，没有漏判分支
- 上游移动了函数调用位置（如 `handle_island_additional()` 被移进 `ensure_island_production_page()`）→ 确认 Fork 的挂钩没被绕过
- 上游改变了数据/路径风格（如 #6026 之后新资产只留 `assets/cn/`）→ 确认新代码没有又写出 `assets/en/...` 的死路径

### 2.3 跑红线自动化检查

```bash
d="C:/Users/zzz/workbuddy-ai/workbuddyai/AzurLaneAutoScript"

# 红线 3：素材引用完整性（基线 46 个 assets.py / ~6228 条 / 断链 0）
# 红线 4：8 个未跟踪文件哈希（清单见 dev-audit-rules.md 红线 4）
# 红线 5：py_compile / json.load
# 红线 2：配置生成器幂等（worktree 隔离）
```

### 2.4 输出分级审查报告

**必须分三级**，每条附 `文件:行号` + 证据命令输出，禁止只写结论：

```markdown
## 阻断级 Bug
- [ ] <文件>:<行> —— <问题> ｜ 证据：<命令与输出> ｜ 影响：<会怎么坏> ｜ 修法：<具体改法>

## 兼容性风险
- [ ] <文件>:<行> —— <风险> ｜ 与 <上游某次改动> 的交互 ｜ 建议验证方式

## 建议优化
- <文件> —— <建议> ｜ 收益
```

**判定口径**：

| 级别 | 含义 |
|---|---|
| 阻断级 | 会导致 ModHandler / 仪表盘失效、配置被清空、运行时崩溃（如素材断链）、GUI 启动失败 |
| 兼容性风险 | 当前能跑，但与上游后续改动叠加后会出问题；或某些服/某些任务下会走到未处理分支 |
| 建议优化 | 一致性、可维护性、性能 |

### 2.5 特别提示：不要误报的两类情况

1. **`config/mod_handler/` 显示为未跟踪** —— 在 `dev` 上它应被忽略；只有切到 `master` 才会变未跟踪（master 的 `.gitignore` 没有 fork 追加的 5 条规则）。这不是问题。
2. **`assets.py` 里 en/jp/tw 指向 `./assets/cn/`** —— 这是上游 #6026 之后的**正确风格**，不是漏改。

---

## 阶段 3：经验实时回写（**强制，结束前必做**）

审查收尾前判断是否产生新知识，有则立即追加：

| 触发条件 | 回写目标 |
|---|---|
| 发现**新的典型错误模式**（某种改法会导致某类故障） | `.agents/memory/dev-audit-rules.md` → 追加红线（写清症状 + 检测命令） |
| 发现**新的保护文件 / 保护区职责变化** | `.agents/memory/fork-override-map.md` → 更新对应分区 |
| 发现某条红线会**误报**，需要补充例外 | `.agents/memory/dev-audit-rules.md` → 在对应红线里补"例外情况" |
| 交接单信息**不足以支撑审查**（缺关键接口说明） | 在报告中明确指出，并建议合并 Agent 补写 `latest-sync-context.md` |

**回写格式要求**：必须具体到真实路径 / 模块名 / 函数名 / 配置项 + 可执行的检测命令，禁止空泛表述。追加到文件末尾的「追加区」。

### 3.2 ★ 铁律：自动推送回 GitHub（**汇报之前的最后一件事**）

账本写完必须立刻回传 GitHub，否则合并 Agent 与 DSH / ZCode / Codex 下次读到的还是旧红线。

```bash
d="C:/Users/zzz/workbuddy-ai/workbuddyai/AzurLaneAutoScript"

# 1) 只暂存经验库，绝不要 git add -A（会带进 8 个不该提交的未跟踪文件）
git -C "$d" add .agents/ AGENTS.md MEMORY.md

# 2) 提交（无改动时会报 nothing to commit，属正常，跳过 3）
git -C "$d" commit -m "chore(agent-memory): sync latest merge/review experience"

# 3) 推送（pack 传输不稳，带 HTTP/1.1）
git -C "$d" -c http.version=HTTP/1.1 push origin HEAD
```

**执行要点**：

- **必须用精确路径 `git add .agents/ AGENTS.md MEMORY.md`**，禁止 `git add -A` / `git add .` —— 仓库里有 8 个用户明确要求不提交的未跟踪文件（`dev_tools/gm_*.py`、`q.py`、两个 GBK 的 .bat）。
- `git push origin HEAD` 推当前分支。若当前不在 `dev`，必须显式 `git push origin HEAD:dev`。
- 推送失败（网络）时**不要静默吞掉**，必须告知用户"经验已本地提交但未推送到 GitHub，需重试 `git push origin HEAD`"。
- 审查若**只发现问题、未产生新知识**（没有新错误模式、没有新保护文件），则无需 commit —— 但仍要在汇报里说明"本次无需回写账本"。

> 汇报时必须说明三件事：**① 回写了哪个账本文件（或"本次无需回写"）② 经验提交的 SHA ③ 推送到了哪个远端分支（`origin/dev`）**。

---

## 附：本仓库已知的高频"看起来像 bug 其实不是"清单

| 现象 | 真相 |
|---|---|
| `config/mod_handler/` 未跟踪 | 切到 master 才这样，dev 上被忽略 |
| en/jp/tw 指向 `./assets/cn/` | 上游 #6026 的标准风格 |
| `config/deploy.yaml` 不受 git 管理 | 被 `.gitignore:6 config/*.yaml` 忽略，`reset --hard` 冲不掉，但也不会随仓库同步 |
| GUI 一直显示"有新版可用"但 HEAD 已是最新 | 更新器状态锁死（`check_update()` 只在 state ∈ (0,"failed","finish") 时重查），重启 GUI 才能恢复 |
| `git diff <上游> <fork>` 里很多"新增行" | fork 独有历史内容（如 Zuosizhu 2023 年的 Button），不是本次改的 |
