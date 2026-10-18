# 双 Agent 实时交接单（latest-sync-context）

> **谁写**：`upstream-merger`（合并 Agent）在每次合并验证通过后**必须**覆盖更新本文件
> **谁读**：`dev-reviewer`（审查 Agent）在审查前**必须**先读本文件
> 本文件只保留**最近一次**同步的上下文；历史批次归档在 `conflict-ledger.md` C-005。

---

## 最近一次同步

| 项 | 值 |
|---|---|
| **同步时间** | 2026-10-17 10:31 +0800 |
| **上游区间** | `56e6ce0b0` → `cb16b4a89`（6 个提交，11 个文件） |
| **dev 合并提交** | `6879ba03d` |
| **master 合并提交** | `482405621`（**用 worktree 完成**，主工作区 HEAD 全程未离开 dev） |
| **ziyong** | `6879ba03d`（与 dev 快进一致） |
| **冲突** | **0 个**（`git merge-tree --write-tree` exit=0） |
| **验证** | 9 个改动 `.py` 全过 py_compile；46 个 assets.py / 6228 条引用 / **断链 0**；生成器幂等；8 个未跟踪文件哈希未变 |

---

## 上游本次关键变动（审查 Agent 必看）

### 1. 新增战斗 UI 资产 `PAUSE_Bullet`（新皮肤暂停按钮）

- 资产：`assets/cn/combat_ui/PAUSE_Bullet.png`（en/jp/tw 均指向 cn）
- 定义：`module/combat_ui/assets.py` → `PAUSE_Bullet = Button(...)`
- 消费方：`module/combat/combat.py`（`handle_combat_pause` 一类的暂停识别链）、`module/exercise/hp_daemon.py`（`PAUSE_*` 列表）
- **审查要点**：如果 Fork 侧有自定义的暂停/退出逻辑（ModHandler 相关），确认新的 `PAUSE_Bullet` 也被纳入判定，否则新皮肤下会漏判 → 卡战斗

### 2. 岛屿慢设备修复（3 处）

| 文件 | 变动 |
|---|---|
| `module/island_handler/assets.py` | 新增 `ISLAND_RESTAURANT_ANIMATION_CHECK`（cn 资产，四服共用） |
| `module/island/business.py` | 进餐厅的判定由 `self.is_in_island_restaurant()` 改为 `self.appear(ISLAND_RESTAURANT_ANIMATION_CHECK, offset=(-170,-20,20,20))` |
| `module/island/production.py` | 生产完成判定由内联 `for _ in self.loop(timeout=5)` 改为调用 `self.ensure_island_production_page()`；并把 `self.handle_island_additional()` **移进** `ensure_island_production_page()` 的循环里 |
| `module/island_handler/restaurant.py` / `shop.py` | 慢设备下的进入与名称 OCR 重试 |

- **审查要点**：`handle_island_additional()` 的位置变化会影响"额外弹窗"处理时机；若 Fork 在岛屿线有挂钩（仪表盘记录），确认没被绕过

### 3. 工具改动 `dev_tools/uiautomator2_screenshot.py`

- 截图改存 **webp**，`cv2.IMWRITE_WEBP_QUALITY = 101`（>100 为无损）
- 修了一个 bug：`ext.lower() == '.webp'` → `'webp'`（原先带点判断永远不成立）
- `similarity >= 1` 时跳过模板匹配（`sim = -1`，强制保存每一帧）

- **审查要点**：这是 `dev_tools/` 下的独立工具，**不参与 ALAS 运行时**，风险低；但注意 `screenshots/` 目录产物格式变了（`.png` → `.webp`）

### 4. 上游整体方向提示

本批延续 2026-10-16 `#6026` 的**素材去重**方向：新增资产一律只有 `assets/cn/` 一份，`assets.py` 里 en/jp/tw 直接指向 cn。**后续上游新增 Button 大概率沿用此风格**，审查时不必再为"为什么 en 指向 cn"困惑。

---

## 给审查 Agent 的重点提示

1. **本批零冲突，但不要因此跳过红线 1**：`module/config/config_updater.py` 的 `min_depth=1`、`module/config/argument/dashboard.yaml` 必须仍在。
2. **`module/combat/combat.py` 与 `module/exercise/hp_daemon.py` 是新增接触点**：这两个文件在 Fork 侧的差异极小（本批之前基本无魔改），但如果将来要挂 ModHandler 的"敏感任务"判定，注意别和上游的 `PAUSE_*` 识别链打架。
3. **岛屿线**（`module/island/`、`module/island_handler/`）属于纯上游区，本批改动可直接接受；重点看 `handle_island_additional()` 移位是否影响仪表盘记录。
4. **下一批上游若继续动 `module/campaign/assets.py`**，务必先读 `conflict-ledger.md` C-001，按变量名逐行判定。

---

## 下一批同步的预判

- `origin/master` 已到 `482405621`，与 `origin/dev`（`6879ba03d`）同步于同一上游点 `cb16b4a89`
- 下次合并前先跑：`git merge-tree --write-tree --name-only dev upstream/master`，exit=0 就放心合
- 若出现多个冲突文件 → 大概率是上游又做结构性重构（如再次批量改 `assets.py` 路径），走 C-001/C-002 范式

### 2026-10-18 复查：上游无新提交，无需合并

```
git rev-list --count cb16b4a89..upstream/master        → 0
git ls-remote upstream refs/heads/master               → cb16b4a89（与本地一致）
```

- 结论：**自 2026-10-17 那次同步以来，上游 `LmeSzinc/AzurLaneAutoScript` 没有任何新提交**。本次任务未产生合并提交。
- 顺带发现并已修正：魔改面由 54 → **62**（新增 8 个 AI 协作体系文件），`fork-override-map.md` 的基线数字已同步更新。
- 下次开工只需重新 `fetch upstream` 确认 HEAD 是否仍为 `cb16b4a89`；变了才需要合并。

---

## 更新记录

| 时间 | 同步区间 | dev 合并提交 | 冲突数 |
|---|---|---|---|
| 2026-10-17 | `56e6ce0b0 → cb16b4a89` | `6879ba03d` | 0 |

---

## GitHub 远端状态（2026-10-18 实测）

### 远端与分支

```
origin    https://github.com/dmsxj3/alasjmdh              （我的 Fork，唯一推送目标）
upstream  https://github.com/LmeSzinc/AzurLaneAutoScript  （上游，只 fetch）
```

| 分支 | SHA | 角色 |
|---|---|---|
| `origin/dev` | `6879ba03d` | **主开发线 + 经验库规范源（canonical）** |
| `origin/ziyong` | `6879ba03d` | 跑 ALAS 的克隆跟踪它；必须等于 dev |
| `origin/master` | `482405621` | **GitHub 默认分支**（`origin/HEAD -> origin/master`）；仪表盘 + 上游，不含 ModHandler |

**没有 `main` 分支。** 经验库以 `origin/dev` 为准。

### 经验库的跨软件读取路径

`.agents/memory/`、`AGENTS.md`、`MEMORY.md` 已提交到 `origin/dev`。其他软件 / 其他分支读取方式：

```bash
git -c http.version=HTTP/1.1 fetch origin --prune
git checkout origin/dev -- .agents/ AGENTS.md MEMORY.md
```

**不需要切分支**，也不需要 clone 第二个工作区。

### 推送纪律（与 `fork-override-map.md` 5.2 一致）

- 全部 `-c http.version=HTTP/1.1`，失败重试
- 只允许 fast-forward；`--dry-run` 出现 `+` 前缀即中止
- `ziyong` 只快进：`git push origin dev:ziyong`
- `master` 只在 worktree 里操作
- **无 GitHub Actions**，推送不触发 CI → 所有验证必须推送前本地完成
- 本 Fork **不提 PR**（历史 0 条），改动直接 push 分支

### 本轮新增的自动化环节

两个 Skill 的账本读写已绑定 GitHub 远端：

| 时点 | 强制动作 |
|---|---|
| 任务开始（阶段 1 最前） | `git fetch origin --prune` + `git checkout origin/dev -- .agents/ AGENTS.md MEMORY.md` |
| 任务结束（最后阶段末） | `git add .agents/ AGENTS.md MEMORY.md` → `git commit -m "chore(agent-memory): sync latest merge/review experience"` → `git push origin HEAD` |

即：**任何 Agent 的经验更新都会实时回到 GitHub，供 DSH / ZCode / Codex 下次读取。**

