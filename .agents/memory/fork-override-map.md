# Fork 魔改保护清单（fork-override-map）

> 仓库：`dmsxj3/alasjmdh`（origin）／ 上游：`LmeSzinc/AzurLaneAutoScript`（upstream/master）
> 本 Fork = [`Zuosizhu/Alas-with-Dashboard`](https://github.com/Zuosizhu/Alas-with-Dashboard) 的仪表盘线 + 自研 ModHandler（悬浮窗倍率控制）线
> **权威口径**：`git diff --name-only upstream/master dev` 当前输出 **62 个文件** = **54 个业务魔改** + **8 个 AI 协作体系文件**（`.agents/**` + `AGENTS.md` + `MEMORY.md`）。这 62 个就是本 Fork 的"魔改面"，其余全部可直接跟随上游。
> 最后核对：2026-10-18（dev = `dd25a9257`，upstream = `cb16b4a89`）
> **历史基线**：2026-10-17 之前为 54 个（业务魔改）；2026-10-18 引入 AI 协作体系后 +8。若数字再次突变，先查是不是新增了本地文件。

---

## 一、核心保护区（保留 ours，禁止被上游覆盖或删除）

这些文件/目录**上游根本不存在**（或语义完全不同），一旦被 `git checkout --theirs` / `reset --hard` 抹掉，ModHandler 与仪表盘会直接失效。

### 1.1 ModHandler 主体（上游无此模块）

| 路径 | 魔改内容 |
|---|---|
| `module/mod_handler/mod_handler.py` | 策略层：按任务决定倍率开关（敏感任务 = 演习 / META / 共斗 / minigame 需关闭）、`already` 判定以设备实时读数为准、fail-closed（读不到状态直接停机推送邮件） |
| `module/mod_handler/mod_overlay.py` | 悬浮窗后端：走 Launcher service 显示/隐藏悬浮窗，实现"零重启"切倍率；含面板存活期、`su` 重建 Service、自愈降级 |
| `module/mod_handler/mod_prefs.py` | `prefs` 后端：直接改写游戏 SharedPreferences 的倍率键值，含写入校验、回读确认、原子写 |
| `module/mod_handler/mod_ui.py` | UI 层：悬浮窗控件定位与点击（滑块式倍率开关） |

### 1.2 ModHandler 配套工具（上游无）

`dev_tools/` 下 10 个文件，全部是自研，**不得删除、不得被上游同名文件覆盖**：

```
dev_tools/mod_discover.py            # 悬浮窗模板发现
dev_tools/mod_handler_astcheck.py    # AST 静态检查
dev_tools/mod_handler_doctor.py      # 自诊断（--apply 原子写）
dev_tools/mod_handler_e2e.py         # 端到端回归
dev_tools/mod_handler_selftest.py    # 自测
dev_tools/mod_handler_test.py        # 单测
dev_tools/mod_handler_testkit.py     # 测试桩（device=None 自建语义）
dev_tools/mod_overlay_selftest.py    # overlay 后端自测
dev_tools/mod_prefs_selftest.py      # prefs 后端自测
dev_tools/mod_ui_selftest.py         # UI 层自测
```

### 1.3 仪表盘（来自 Zuosizhu/Alas-with-Dashboard，上游无）

| 路径 | 魔改内容 |
|---|---|
| `module/config/argument/dashboard.yaml` | 定义 Dashboard 组：`Dashboard: [Oil, Coin, Gem, Pt, Cube, ...]`，**值是 list 不是 dict** —— 这是 `config_updater.py` 必须写 `min_depth=1` 的原因 |
| `module/config/config_updater.py` | 关键修复（**第 176–181 行**）：`deep_iter(dashboard_and_task, min_depth=1, depth=3)`。漏掉 `min_depth=1` 会导致跑一次生成器就**清空整组 Dashboard 配置** |
| `module/log_res/log_res.py` | 仪表盘资源记录（Oil/Coin/Gem/Pt/Cube 的 Value/Color/Record） |
| `assets/gui/css/alas.css`、`alas-pc.css`、`alas-mobile.css`、`dark-alas.css`、`light-alas.css` | 仪表盘样式（上游同名文件不含 dashboard 布局，**不能整体采用 theirs**） |

### 1.4 Fork 独有资产（上游无，删了就断链）

```
assets/cn/campaign/OCR_COIN_LIMIT.png
assets/cn/campaign/OCR_GEM.png
assets/cn/campaign/OCR_OIL_LIMIT.png
```

### 1.5 仓库级约定文件

| 路径 | 说明 |
|---|---|
| `.gitignore` | 末尾 fork 追加 5 条忽略规则：`config/mod_handler/`、`.git_corrupt_bak_*/`、`_modhandler_bak/`、`_alas_wip_backup_*/`、`dev_tools/.mod_snapshots/`。**master 分支没有这 5 条**，切分支时本地产物会从"被忽略"变"未跟踪"，这是正常的，别往 master 加规则 |
| `alas.py` | 启动入口的 ModHandler 挂钩 |
| `README.md` | 致谢头部 + 已合并上游的说明 |

### 1.6 AI 协作体系（2026-10-18 新增，上游完全不存在）

这 8 个文件是 Fork 独有的**经验库与 Agent 规范**，构成"双 Skill + 动态记忆账本"。**任何合并都不得删除或覆盖它们**（`git checkout --theirs` / `reset --hard` 对它们无效，但 `git clean -fd` 会删掉未跟踪版本，注意）。

| 路径 | 作用 |
|---|---|
| `AGENTS.md` | 跨工具入口（DSH / ZCode / Codex 自动读取） |
| `MEMORY.md` | 与 `AGENTS.md` **逐字节一致**（WorkBuddy 自动读取）。改一份必须 `cp` 同步另一份 |
| `.agents/skills/upstream-merger/SKILL.md` | 合并上游 Agent 规范（4 阶段，含 GitHub 双向同步铁律） |
| `.agents/skills/dev-reviewer/SKILL.md` | dev 审查 Agent 规范（3 阶段） |
| `.agents/memory/fork-override-map.md` | 本文件 |
| `.agents/memory/conflict-ledger.md` | 冲突判例库 |
| `.agents/memory/dev-audit-rules.md` | 审查红线 9 条 |
| `.agents/memory/latest-sync-context.md` | 双 Agent 交接单 |

**维护约束**：

- 规范源是 **`origin/dev`**（不是 `master`、不是 `main`）
- `AGENTS.md` 与 `MEMORY.md` 必须逐字节一致：改完用 `diff -q` 或比对 `git hash-object` 校验
- 这 8 个文件**会出现在魔改面统计里**（+8），所以 `git diff --name-only upstream/master dev` 应为 **62**；若回落到 54，说明经验库被丢了

---

## 二、高危耦合区（两边都改，逐行人工/AI 比对）

原则：**先看上游这一段改了什么语义，再决定逐行采用哪边**，禁止整文件选边。

### 2.1 配置五源（最容易撞，且撞了会导致 GUI 缺项/KeyError）

```
module/config/argument/argument.yaml      # 主定义（顶层是「组」不是「任务」）
module/config/argument/args.json          # 生成物（任务→组→参数）
module/config/config_generated.py         # 生成物（组_参数，不含任务）
config/template.json                      # 生成物（新建实例模板，值必须是纯标量）
module/config/i18n/{zh-CN,zh-TW,en-US,ja-JP}.json   # 显示名/帮助
```

- Fork 侧：新增 `ModHandler` / `Dashboard` 相关组与词条
- 上游侧：频繁新增活动入口（`event_*_cn`）、归一化舰船名、改 option 列表
- **合并后必须跑生成器验证幂等**（见 `dev-audit-rules.md` 红线 2）

### 2.2 任务与 GUI 注册表

```
module/config/argument/task.yaml     # 组→任务→组名；fork 加 ModHandler 任务
module/config/argument/menu.json     # GUI 菜单；fork 的 ModHandler 是 Tool 下的 task
module/config/argument/gui.yaml      # GUI 词条
module/webui/app.py                  # fork 改：ModHandler 面板 / 更新器页面
module/webui/pin.py
module/webui/widgets.py
module/config/utils.py
```

### 2.3 仪表盘/ModHandler 的业务挂钩（上游也在持续改这些文件）

```
module/campaign/campaign_event.py    # ModHandler 按任务事件挂钩
module/campaign/campaign_status.py
module/campaign/run.py
module/gacha/gacha_reward.py         # 仪表盘：抽卡后刷新
module/shop/shop_status.py           # 仪表盘：商店金币
module/os_handler/action_point.py    # 仪表盘：大世界行动力
module/os_handler/os_status.py
module/raid/raid.py                  # 仪表盘：PT 记录
module/coalition/coalition.py
module/combat/auto_search_combat.py  # 仪表盘：两战之间记录油/币
```

### 2.4 ★ 头号冲突热点

```
module/campaign/assets.py
```

- Fork 侧（Zuosizhu 2023 年引入）：`OCR_COIN_LIMIT`、`OCR_GEM`、`OCR_OIL_LIMIT` 三个 Button
- 上游侧：`Remove en/jp/tw assets identical to cn (#6026)` 把大量 `file={...}` 的 en/jp/tw 改指 `./assets/cn/`
- **两者落在文件同一段相邻行 → 已实战冲突 1 次（2026-10-16）**，解决范式见 `conflict-ledger.md` 判例 C-001

---

## 三、纯上游区（直接采用 theirs）

除上述 54 个文件之外的一切，**没有本地改动，可直接跟随上游**。典型高频变动目录：

```
module/device/          module/map/            module/combat_ui/
module/ui/              module/ui_white/       module/tactical/
module/research/        module/island/         module/island_handler/
module/dorm/            module/private_quarters/  module/equipment/
module/os_shop/         module/shop/base.py    campaign/**（除 assets.py）
module/handler/         module/meeting/        module/event/
```

合并这些文件时**不要犹豫**：`git checkout --theirs -- <path>` 即可。

---

## 四、判定流程（合并前必跑）

```bash
# 1. 取当前魔改面（应 = 62 个文件 = 54 业务魔改 + 8 AI 协作体系；数量突变说明保护清单变了）
git diff --name-only upstream/master dev | sort > /tmp/fork_files.txt

# 2. 取上游本次改动
git diff --name-only <上次同步点> upstream/master | sort > /tmp/up_files.txt

# 3. 冲突候选 = 交集
comm -12 /tmp/fork_files.txt /tmp/up_files.txt

# 4. 对交集里的每个文件，先归类（核心保护 / 高危耦合），再决定合并策略
```

---

## 五、GitHub 远端架构与交互经验（2026-10-18 实测）

### 5.1 远端与分支对应关系

| remote | URL | 用途 |
|---|---|---|
| `origin` | `https://github.com/dmsxj3/alasjmdh` | **我的 Fork**，所有推送目标 |
| `upstream` | `https://github.com/LmeSzinc/AzurLaneAutoScript` | 上游，只 fetch 不 push |

| 远端分支 | 内容 | 角色 |
|---|---|---|
| `origin/dev` | 仪表盘 + ModHandler + 上游同步 | **主开发线，也是 `.agents/` 经验库的规范分支（canonical）** |
| `origin/ziyong` | 必须等于 `dev` | 实际跑 ALAS 的克隆跟踪它；只做快进 |
| `origin/master` | 仪表盘 + 上游，**不含 ModHandler** | **GitHub 默认分支**（`origin/HEAD -> origin/master`） |

> ⚠️ **本仓库没有 `main` 分支。** 讨论"主分支"时：若指 GitHub 默认分支 → `master`；若指开发主线和经验库来源 → **`dev`**。`.agents/` 与 `AGENTS.md`/`MEMORY.md` 统一以 **`origin/dev`** 为准。

实测（2026-10-18）：
```
6879ba03d  refs/heads/dev
482405621  refs/heads/master
6879ba03d  refs/heads/ziyong
```

### 5.2 推送（Push）注意事项与踩坑

| 项 | 结论 |
|---|---|
| **pack 传输不稳** | 到 github.com 的传输经常中断，`fetch` / `push` 必须加 `-c http.version=HTTP/1.1`，失败要重试（历史上多次靠这个才成功） |
| **代理** | 本仓库**本地/全局都没有配 `http.proxy`**，直连可用。`config/deploy.yaml` 里的 `GitProxy: http://127.0.0.1:7897` 是 **ALAS 自己的更新器**用的，不影响手工 git 命令 |
| **只做快进** | `dev` / `ziyong` / `master` 全部只允许 fast-forward。`push --dry-run` 输出**带 `+` 前缀 = 强推**，出现即中止并排查 |
| **ziyong 同步方式** | 只能 `git branch -f ziyong dev && git push origin dev:ziyong`。**不要 merge、不要 rebase** |
| **master 同步方式** | 必须 `git worktree add` 到临时目录做 merge + push，**绝不切主工作区 HEAD**（否则 GUI 更新器误报并锁死） |
| **git 版本混用** | 仓库自带 `toolkit/Git/mingw64/bin/git.exe` = **2.28.0**（merge 策略显示 `recursive`）；系统 git = **2.55.0**（显示 `ort`）。日志里看到 `recursive` 说明是 ALAS 更新器或旧 git 在操作，不是人手 |
| **提交身份** | `user.name = dmsxj3`，`user.email = dmsxj3@users.noreply.github.com`（本地已配） |

### 5.3 PR 与 GitHub Actions

- **本 Fork 从不提 PR**：`gh pr list --repo dmsxj3/alasjmdh --state all` 返回 **0 条**。所有改动都是**直接 push 到分支**。上游的 PR（如 #6056 / #6052 / #6039 / #6037 / #6036 / #6035）只是被我们合并进来的内容来源。
- **本仓库没有 GitHub Actions**：`.github/workflows/` 不存在 → 推送不会触发任何 CI，**没有自动化兜底**。这意味着所有验证（素材引用完整性、生成器幂等、py_compile）都必须在**推送前本地做完**，不能指望 CI 拦住。
- `gh` CLI 已安装（v2.100.0），已认证为 `dmsxj3`，token scope 含 `repo` / `workflow` / `gist` / `read:org`。

### 5.4 跨分支读经验库的正确姿势

经验库（`.agents/`、`AGENTS.md`、`MEMORY.md`）以 `origin/dev` 为唯一规范源。在**任何分支**（含 `master`、`ziyong`、临时 worktree）上工作时，都用：

```bash
git -c http.version=HTTP/1.1 fetch origin --prune
git checkout origin/dev -- .agents/ AGENTS.md MEMORY.md
```

这样不需要切分支就能读到 GitHub 上最新版本。

> ⚠️ 该命令会**覆盖本地未提交的同名改动**。若手上有未推送的账本修改，先 commit 或 stash。

