# 双 Agent 实时交接单（latest-sync-context）

> **谁写**：`upstream-merger`（合并 Agent）在每次合并验证通过后**必须**覆盖更新本文件
> **谁读**：`dev-reviewer`（审查 Agent）在审查前**必须**先读本文件
> 本文件只保留**最近一次**同步的上下文；历史批次归档在 `conflict-ledger.md` C-005。

---

## 最近一次同步

| 项 | 值 |
|---|---|
| **同步时间** | 2026-10-23 11:54 +0800（master 补同步 12:30，worktree 完成） |
| **上游区间** | `cb16b4a89` → `4d88c22d8`（**29 个提交，61 个文件**） |
| **dev 合并提交** | `e6dda2ca2` |
| **master 合并提交** | `b203c937d`（2026-10-23 用 **worktree** 完成，主工作区 HEAD 全程未离开 dev；验证：零冲突、3 Button/dashboard.yaml 保留、7216 引用断链 0、py 编译过；push 预检 fast-forward 后推送，`482405621..b203c937d`） |
| **ziyong** | `14f4e00d8`，**落后 dev**（待快进） |
| **冲突** | **0 个**（dev 与 master 两侧均零冲突；冲突候选 7 个全在配置五源区，全部自动合并成功） |
| **冲突候选** | 7 个（全在配置五源区，见下），但全部自动合并成功 |
| **验证** | 48 个改动 `.py` + 6 个 `.json` 全过；45+ 个 assets.py / **7216 条引用 / 断链 0**（dev 与 master 实测一致）；**生成器幂等**（dev 侧 worktree 实测 exit=0 + `update_file:` 收尾 + status 空）；8 个未跟踪文件哈希未变 |
| **合并规模** | 61 files changed, +3016 / -286 |

### ⚠️ 审查发现（2026-10-23，未修，待用户裁决）

**master 分支缺 `min_depth=1`**：`module/config/config_updater.py:177` 是 `deep_iter(dashboard_and_task, depth=3)`（无 min_depth），而 dev:179 有。该修复 2026-09-24 由 `c651aae36` 只提交在 dev 线，**从未同步到 master**（`git log -S 'min_depth=1' master -- module/config/config_updater.py` 为空）。master 上有 `dashboard.yaml` + 177 行遍历 + 178 行 `'Dashboard' in path` 分支 → **在 master 检出态跑一次生成器会清空 Dashboard 配置组**（红线 1 头号坑在 master 上成立）。修法：`git checkout master && git cherry-pick c651aae36`（或等价最小 diff）。当前实际跑 ALAS 的克隆在 dev/ziyong，暂不受影响。

---

## 上游本次关键变动（审查 Agent 必看）

### 1. 新活动 `event_20261022_cn`

```
campaign/event_20261022_cn/campaign_base.py  (+25)
campaign/event_20261022_cn/sp.py             (+97)
campaign/event_20261022_cn/t1.py ~ t6.py     （6 个关卡）
module/eventstory/eventstory.py              (+1)  ← eventstory 支持
```

- 配置侧：`args.json` 的 Event `option_cn/en/jp/tw` 由 `event_20260908_cn` 轮换为 `event_20261022_cn`（TW 保留了旧列表，见 diff）
- **注意**：活动列表**不是**写在 `argument.yaml` 里的，而是生成器**扫描 `campaign/` 目录**得出 → 合并后跑生成器会自动对齐，不要手工改 `args.json`

### 2. 新战争档案 ×2

```
campaign/war_archives_20241024_cn/   （campaign_base + t1~t6）
campaign/war_archives_20251023_cn/   （campaign_base + t1~t6）
module/war_archives/assets.py        (+2)
module/war_archives/dictionary.py    (+2)   ← Sleeping Sea / Islas de Libertad 词条
campaign/campaign_war_archives/campaign_base.py (+7/-1)
```

- 新增资产：`assets/cn/war_archives/TEMPLATE_TEMPESTA_AND_ISLAS_DE_LIBERTAD.png`、`TEMPLATE_TEMPESTA_AND_THE_SLEEPING_SEA.png`
- 另修：滚动战争档案时按滑动次数而非截图次数限流（`f4771761b`）

### 3. Chapter T & SP

```
a14b1e56a Add: Chapter T & SP
25f8baa77 Fix: map configs in chapter T
dev_tools/map_extractor.py (+5/-2)
```

### 4. 岛屿线大改（本批最大风险区）

| 文件 | 变动 |
|---|---|
| `module/island_handler/exchange.py` | **+305/-48**，重构 |
| `module/island_handler/production_plan_calculator.py` | +55/-17 |
| `module/island_handler/recipe.py` | +59/-5 |
| `module/island_handler/shop.py` | +58/-11 |
| `module/island_handler/shop_ui.py` | +27（新拆分文件） |
| `module/island/production.py` | +22/-1 |
| `module/island/collect.py` | +7/-1（慢机 dock 扫描重试） |
| `module/os_shop/shop.py`、`module/shop/clerk.py`、`module/private_quarters/private_quarters.py` | 小修 |

行为变化：保护库存、鱼→鱼肉兑换、牧场次产物按 3 批摊销、硬地板生产优先级、商店名称 OCR 正则清洗 + 只在完全识别不到时重试。

**新增资产**：`assets/island/character/{Anchorage,Leonardo_da_Vinci,Mogador}.png`

### 5. 地图 / 其他

- `module/map/fleet_bar.py` **+70/-30**、`module/map/map_fleet_preparation.py`
- memu 模拟器 `MAP_PREPARATION` 缺模糊背景修复、半加载 infobar 遮挡舰队选项修复
- `module/campaign/campaign_ui.py`、`module/campaign/run.py`（+6）—— **run.py 在 Fork 侧也被改过（ModHandler 挂钩），本次自动合并成功**

### 6. ★ 上游新增单元测试目录

```
tests/island_handler/test_exchange.py
tests/island_handler/test_production_plan_calculator.py
tests/island_handler/test_recipe_fish.py
tests/island_handler/test_recipe_modes.py
tests/island_handler/test_shop_product_name_ocr.py
tests/island_handler/test_shop_protected_items.py
```

上游开始给岛屿逻辑补测试。**Fork 侧没有对应机制**，审查时可留意这些测试是否需要跟随 ModHandler 的语义调整（目前不冲突）。

---

## 给审查 Agent 的重点提示

1. **配置五源全部被两边改过**（`args.json`、4 个 i18n、`template.json`、`campaign/run.py`），但自动合并成功且**生成器幂等通过** → 五源一致，不需要手工修。
2. **`module/campaign/run.py` 是 Fork 的 ModHandler 挂钩点**，本批上游也改了它（+6）。重点看 ModHandler 的任务事件挂钩是否被绕开。
3. **岛屿线改动极大**（exchange 重构 +305/-48）。如果 Fork 在岛屿有仪表盘记录挂钩（`module/log_res/log_res.py` 不在本批改动里，但岛屿资源记录可能受影响），需要实测。
4. **新增 `tests/` 目录**：审查时可以顺手跑一下 `tests/island_handler/`，作为岛屿改动的额外回归。
5. 本批零冲突，但**冲突候选有 7 个** —— 别被"零冲突"麻痹，配置区仍需看生成器结果。

---

## 下一批同步的预判

- `upstream/master` = `4d88c22d8`，`origin/dev` = `e6dda2ca2`
- `origin/master` = `482405621`，**落后上游 29 个提交**（下次要单独同步，务必用 worktree）
- `origin/ziyong` = `14f4e00d8`，**落后 dev**（待快进）

**下次开工第一步**：`git fetch upstream && git rev-parse upstream/master`，仍是 `4d88c22d8` 就说明上游没动。

### ⚠️ 方法论修正（2026-10-23 发现，已写入 fork-override-map 第四节）

计算"魔改面"**不能用 `git diff --name-only upstream/master dev`** —— 在未同步状态下，上游的新增文件会被算进"魔改面"，导致数字虚高（本次虚高到 116）。正确做法是用 **merge-base**：

```bash
base=$(git merge-base dev upstream/master)
git diff --name-only $base dev            # fork 侧改动（本次 62）
git diff --name-only $base upstream/master # 上游改动（本次 61）
comm -12 ...                               # 真正的冲突候选
```

---

## 更新记录

| 时间 | 同步区间 | 提交数 | dev 合并提交 | 冲突数 |
|---|---|---|---|---|
| 2026-10-23 | `cb16b4a89 → 4d88c22d8` | 29 | `e6dda2ca2` | 0（候选 7） |
| 2026-10-17 | `56e6ce0b0 → cb16b4a89` | 6 | `6879ba03d` | 0 |
| 2026-10-18（复查） | 无新提交 | 0 | — | — |

---

## GitHub 远端状态（2026-10-23 实测）

```
origin    https://github.com/dmsxj3/alasjmdh              （我的 Fork，唯一推送目标）
upstream  https://github.com/LmeSzinc/AzurLaneAutoScript  （上游，只 fetch）
```

| 分支 | SHA | 角色 |
|---|---|---|
| `origin/dev` | `e6dda2ca2` | **主开发线 + 经验库规范源（canonical）** |
| `origin/ziyong` | `14f4e00d8` | 跑 ALAS 的克隆跟踪它；必须等于 dev（待快进） |
| `origin/master` | `482405621` | **GitHub 默认分支**；仪表盘 + 上游，不含 ModHandler |

**没有 `main` 分支。** 经验库以 `origin/dev` 为准。

### 经验库的跨软件读取路径

```bash
git -c http.version=HTTP/1.1 fetch origin --prune
git checkout origin/dev -- .agents/ AGENTS.md MEMORY.md
```

**不需要切分支**，也不需要 clone 第二个工作区。

### 推送纪律

- 全部 `-c http.version=HTTP/1.1`，失败重试
- 只允许 fast-forward；`--dry-run` 出现 `+` 前缀即中止
- `ziyong` 只快进：`git push origin dev:ziyong`
- `master` 只在 worktree 里操作
- **无 GitHub Actions**，推送不触发 CI → 所有验证必须推送前本地完成
- 本 Fork **不提 PR**（历史 0 条），改动直接 push 分支
