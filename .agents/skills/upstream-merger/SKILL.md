---
name: upstream-merger
description: 把 AzurLaneAutoScript 上游（LmeSzinc/AzurLaneAutoScript）同步进本 Fork 的 dev 分支。当用户说「合并上游」「同步 Lme 更新」「合到 dev」「上游又更新了」时使用。强制要求先读记忆账本、按保护清单分级处理冲突、合并后写交接单并回写经验。
---

# 上游合并 Agent 规范（upstream-merger）

## 适用仓库

| 项 | 值 |
|---|---|
| 主开发克隆 | `C:\Users\zzz\workbuddy-ai\workbuddyai\AzurLaneAutoScript` |
| origin | `https://github.com/dmsxj3/alasjmdh` |
| upstream | `https://github.com/LmeSzinc/AzurLaneAutoScript` |
| 合并目标分支 | `dev`（主工作区，HEAD 保持在此） |

---

## 阶段 1：读取记忆（**执行任何 git 命令之前**）

### 1.0 ★ 铁律：先从 GitHub 远端拉取最新经验（**第一件事，不可跳过**）

经验库以 **`origin/dev`** 为唯一规范源。本地账本可能被别的软件（WorkBuddy / DSH / ZCode / Codex）在别的分支上更新过，**必须先同步**，否则你会按过期经验裁决冲突。

```bash
d="C:/Users/zzz/workbuddy-ai/workbuddyai/AzurLaneAutoScript"

# 1) 拉远端（pack 传输不稳，必须带 HTTP/1.1，失败重试 2-3 次）
git -C "$d" -c http.version=HTTP/1.1 fetch origin --prune

# 2) 用 GitHub 上的最新经验库覆盖本地（不切分支，当前在哪都行）
git -C "$d" checkout origin/dev -- .agents/ AGENTS.md MEMORY.md
```

**注意事项**：

- 该 `checkout` 会**覆盖本地未提交的同名改动**。若本地账本有未推送修改，先 `git stash` 或 commit。
- 首次执行时若远端还没有 `.agents/`（历史首次），命令会报 `pathspec ... did not match`，属正常，忽略继续。
- 本仓库**没有 `main` 分支**；规范源是 `origin/dev`，不要写成 `origin/main`。
- 若 `fetch` 失败，**必须明确告知用户"经验库未能从 GitHub 同步，以下操作基于本地版本"**，不得静默继续。

### 1.1 读取账本

必须按顺序读取（读取的是刚同步下来的最新版本）：

1. `.agents/memory/fork-override-map.md` —— 确认核心保护区 / 高危耦合区 / 纯上游区；**特别看第五节「GitHub 远端架构与交互经验」**
2. `.agents/memory/conflict-ledger.md` —— 查历史判例，尤其 **C-001 `module/campaign/assets.py`** 与 **C-002 批量删素材**

> 跳过这一步直接合并 = 违规。历史判例里写着的裁决逻辑（逐变量判定、素材引用校验）必须照做。

然后探明仓库状态：

```bash
git rev-parse --abbrev-ref HEAD                 # 必须是 dev
git rev-parse --is-shallow-repository           # 必须 false，浅克隆要先补全历史
git -c core.quotepath=false status --porcelain  # 只应有 8 个已知未跟踪文件
git branch -vv
```

---

## 阶段 2：分级合并

### 2.1 抓上游 + 定基准

```bash
git -c http.version=HTTP/1.1 fetch --no-tags upstream master
# 上次同步点 = dev 上最近一次 upstream merge 的第二个父提交
git log -1 --format='%h %ad %s' --date=short <上次同步点>
```

### 2.2 分析 + 冲突预演（**不碰工作区**）

```bash
git log --no-merges --format='@@ %h %ad %s' --date=short <base>..upstream/master
git diff --numstat <base> upstream/master | awk '$3 !~ /^assets\//'
git merge-tree --write-tree --name-only dev upstream/master   # exit 0 = 无冲突
```

`exit=0` → 直接进 2.4。
`exit=1` → 进入 2.3 分级处理。

### 2.3 按保护清单分级处理冲突

先算冲突候选：

```bash
git diff --name-only upstream/master dev | sort > /tmp/fork_files.txt
git diff --name-only <base> upstream/master | sort > /tmp/up_files.txt
comm -12 /tmp/fork_files.txt /tmp/up_files.txt
```

对每个实际冲突文件，按 `fork-override-map.md` 归类：

| 归类 | 处理方式 |
|---|---|
| **核心保护区**（`module/mod_handler/*`、`dev_tools/mod_*`、`module/config/argument/dashboard.yaml`、`module/config/config_updater.py` 的 `min_depth=1`、`assets/gui/css/*`、fork 独有资产） | **保留 ours**。上游不可能有这些文件；若上游"删除了"它们，说明是误判，必须恢复 |
| **高危耦合区**（配置五源、`module/campaign/assets.py`、`module/webui/app.py`、仪表盘挂钩文件） | **逐行/逐变量判定**，禁止整文件选边。判据见 `conflict-ledger.md` |
| **纯上游区**（其余全部） | `git checkout --theirs -- <path>`，不要犹豫 |

**严禁**：
- 对核心保护区执行 `git checkout --theirs`
- 用 `git checkout --ours` / `--theirs` 一把梭处理整个高危耦合文件
- 在没读判例的情况下手工拼冲突

### 2.4 备份 + 合并

```bash
git branch backup-dev-<YYYYMMDD> dev
# 记录未跟踪文件哈希（必须加 core.quotepath=false，否则中文名会变八进制转义）
git -c core.quotepath=false status --porcelain | grep '^??' | sed 's/^?? //' > /tmp/ut.txt
while IFS= read -r f; do [ -f "$f" ] && printf '%s  %s\n' "$(git hash-object "$f")" "$f"; done < /tmp/ut.txt

git merge upstream/master --no-edit -m "<描述性合并信息：区间 + 提交数 + 按模块要点>"
```

### 2.5 合并后验证（逐条做，缺一不可）

| # | 检查 | 命令 / 判据 |
|---|---|---|
| 1 | fork 独有文件零改动 | 对 `comm -23 fork_files up_files` 逐个 `git diff --quiet backup..HEAD -- <f>` |
| 2 | 未跟踪文件哈希未变 | 重算 8 个文件 `git hash-object` 与备份比对 |
| 3 | 语法 | 每个改动 `.py` 跑 `py_compile`；每个改动 `.json` 跑 `json.load` |
| 4 | **素材引用完整性** | 扫描全部 `assets.py` 的 `./assets/...` → `os.path.exists`。基线 **46 个文件 / ~6228 条 / 断链 0** |
| 5 | **配置生成器幂等** | worktree 隔离跑 `toolkit/python.exe -m module.config.config_updater`，`git status` 必须为空，且日志有 `update_file:` 收尾 |
| 6 | 清理临时产物 | 删 worktree（`rm -rf` + `git worktree prune`）、临时脚本 |

### 2.6 推送

```bash
git -c http.version=HTTP/1.1 push --dry-run origin dev   # 确认是 fast-forward（无 + 前缀）
git -c http.version=HTTP/1.1 push origin dev
git ls-remote origin refs/heads/dev
```

**只推 dev**。`ziyong` 要单独确认后再快进：`git branch -f ziyong dev && git push origin dev:ziyong`。

---

## 阶段 3：生成交接单

合并验证通过后，**覆盖更新** `.agents/memory/latest-sync-context.md`，至少写清：

1. 同步时间、上游区间（`<base> → <head>`）、提交数 / 文件数
2. `dev` 与 `master` 的合并提交 SHA
3. 冲突数量与裁决摘要（零冲突也要写"0"）
4. **上游改了哪些关键接口** —— 按模块列出：新增/改名的资产常量、改动的函数签名、行为变化（例如"`handle_island_additional()` 被移进 `ensure_island_production_page()`"）
5. 给审查 Agent 的重点提示（哪些文件是新增接触点、哪些红线要重点看）
6. 更新"更新记录"表格

这一步是审查 Agent 的唯一信息来源，**写得含糊等于没写**。

---

## 阶段 4：经验实时回写（**强制，不可跳过**）

**在向用户汇报结果之前**，判断本次是否产生新知识，有则立即追加：

| 触发条件 | 回写目标 |
|---|---|
| 遇到**新的冲突模式**（新的冲突文件、新的冲突类型如 modify/delete、rename） | `.agents/memory/conflict-ledger.md` → 追加 `C-00X` 判例（文件 / 原因 / 标准解决范式） |
| 新增或改变了**本地魔改文件**（新加文件、改了保护区的职责） | `.agents/memory/fork-override-map.md` → 更新对应分区，并核对"魔改面文件数"是否变化 |
| 发现新的**验证手法 / 踩坑**（例如某个检查会误判） | `.agents/memory/dev-audit-rules.md` → 追加红线 |
| 上游做了**结构性重构**（批量改路径、目录搬迁） | 同时更新 `conflict-ledger.md`（新增判例）与 `latest-sync-context.md`（提示后续批次） |

**回写格式要求**：必须具体到真实路径 / 模块名 / 函数名 / 配置项，禁止"注意兼容性"这类空话。追加到文件末尾的「追加区」，不要覆盖历史条目。

### 4.2 ★ 铁律：自动推送回 GitHub（**汇报之前的最后一件事**）

账本写完必须立刻回传 GitHub，否则其他软件（DSH / ZCode / Codex）和其他分支读到的是旧经验。

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
- `git push origin HEAD` 推的是**当前分支**。若当前不在 `dev`（例如在 master worktree 里），必须显式写 `git push origin HEAD:dev`，确保经验库落在规范分支上。
- 推送失败（网络）时**不要静默吞掉**，必须告知用户"经验已本地提交但未推送到 GitHub，需重试 `git push origin HEAD`"。
- 若合并提交本身也还没推（阶段 2.6 没做），**先推代码再推账本**，避免账本先于代码到达远端。

> 汇报时必须说明三件事：**① 回写了哪个账本文件 ② 经验提交的 SHA ③ 推送到了哪个远端分支（`origin/dev`）**。

---

## 命令速查（本机已验证可用）

```bash
d="C:/Users/zzz/workbuddy-ai/workbuddyai/AzurLaneAutoScript"

# 抓上游（pack 传输不稳，必须加 http.version=HTTP/1.1，必要时重试）
git -C "$d" -c http.version=HTTP/1.1 fetch --no-tags upstream master

# 冲突预演（不碰工作区）
git -C "$d" merge-tree --write-tree --name-only dev upstream/master

# 生成器幂等验证
git -C "$d" worktree add "C:/Users/zzz/workbuddy-ai/_gen" HEAD
cd "C:/Users/zzz/workbuddy-ai/_gen" && "$d/toolkit/python.exe" -m module.config.config_updater
git status --porcelain
```

**工具版本辨识**：仓库自带 `toolkit/Git/mingw64/bin/git.exe` = 2.28.0（merge 策略显示 `recursive`）；系统 git = 2.55（显示 `ort`）。看到 `recursive` 说明是 ALAS 自己的更新器或旧 git 在操作。
