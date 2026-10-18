# dev 分支审查红线（dev-audit-rules）

> 审查对象：`dev` 分支相对上一次基线（`origin/dev` 的上一版、或 `backup` 点）的改动，以及刚合并进来的上游批次。
> 输出要求：**分级报告** —— `阻断级 Bug` / `兼容性风险` / `建议优化`，每条附文件:行号与证据命令。
> 本清单来自 2026-09 ~ 2026-10 的 4 次真实合并与审查，每条都是踩过的坑。

---

## 红线 1：魔改接口完整性（阻断级）

合并/改动后必须确认 ModHandler 与仪表盘的对外接口没被削弱：

| 检查点 | 证据 |
|---|---|
| `module/mod_handler/{mod_handler,mod_overlay,mod_prefs,mod_ui}.py` 四个文件都在且可 import | `py_compile` + 类名/方法名 grep |
| `module/config/argument/dashboard.yaml` 仍存在，且 `Dashboard:` 的值仍是 **list** | `git cat-file -e HEAD:module/config/argument/dashboard.yaml` |
| `module/config/config_updater.py` 的 **`min_depth=1`** 仍在（约第 176–181 行） | `grep -n "min_depth=1" module/config/config_updater.py` |
| `dev_tools/mod_*.py` 10 个工具都在 | `ls dev_tools/ \| grep '^mod_'` |
| `module/config/argument/task.yaml` 里 ModHandler 任务仍在 | `grep -n "ModHandler" module/config/argument/task.yaml` |

> **`min_depth=1` 是本 Fork 最脆的一行**：`dashboard.yaml` 的值是 list，而 `deep_iter` 只把 dict 放进队列；漏掉它，`'Dashboard' in path` 分支变成死代码，**跑一次生成器就清空整组 Dashboard 配置**。

---

## 红线 2：配置五源一致性（阻断级）

改过 `argument.yaml` / `task.yaml` / `dashboard.yaml` / `gui.yaml` / `menu.json` / i18n 之后，**必须**验证生成器幂等。

```bash
git worktree add "C:/Users/zzz/workbuddy-ai/_gen" HEAD
cd "C:/Users/zzz/workbuddy-ai/_gen"
<主仓库>/toolkit/python.exe -m module.config.config_updater
git status --porcelain          # 必须为空
```

**必须同时检查退出码与完整日志**：生成器可能在 `./campaign/Readme.md` 处抛 `PermissionError` 提前退出，此时 `git status` 也是空的 —— 会被误读成"零差异"。看日志里有没有 `update_file:` 这一段收尾。

五源对应关系（改一个要同步其余）：

| 来源 | 角色 | 注意 |
|---|---|---|
| `module/config/argument/argument.yaml` | 主定义 | **顶层是「组」不是「任务」** |
| `module/config/argument/args.json` | 生成物 | 任务→组→参数 |
| `module/config/config_generated.py` | 生成物 | 命名是 `组_参数`，**不含任务** |
| `config/template.json` | 生成物 | 值**必须是纯标量**（不能是带类型的 dict） |
| `module/config/i18n/*.json` | 显示名/帮助 | 任务→组→参数 |

---

## 红线 3：素材引用完整性（阻断级，仅在动了 assets 时）

只要上游批次里出现 `assets/` 的 **D（删除）** 或大量 `assets.py` 的路径改写，就必须跑：

```
扫描 module/ + campaign/ 下全部 assets.py 的 ./assets/... 引用 → os.path.exists
```

基线：**46 个 assets.py / 6220–6228 条引用 / 断链 0**。断链 > 0 即阻断，因为 `file={...}` 是字符串路径，Python 不报错，**只有 ALAS 跑到那个功能时才炸**。

---

## 红线 4：未跟踪文件不得被动（阻断级）

用户明确要求**不提交**的 8 个文件，合并/切分支前后哈希必须完全一致：

```
7783ee1cdf4133f9213beee07d750aac2d9674ce  dev_tools/gm_capture.py
5adcb87a162225405dae23cd8338d32c5ef0d8ab  dev_tools/gm_drive.py
9c207c8387cec4002e1e4a41ec37f820c982d99e  dev_tools/gm_ocr.py
d455f7478b312089d6fc4145f892c580eb795bdb  dev_tools/gm_s.py
a55313d23d3661ec0465795311e739cec54501ed  dev_tools/gm_scan.py
4726ccc67f392d140b6f028f9b5319995c71f642  dev_tools/q.py
062c3144aba23b878c6333ec268a6d0d3df744bc  启动ALAS-16384.bat      ← GBK 编码
cdff88450f85ef0b093c6d65657e6bdcd9919cad  提交到dev.bat           ← GBK 编码
```

校验：`git hash-object <file>` 逐个比对。**注意** `git status --porcelain` 默认会把中文名转义成八进制，必须加 `-c core.quotepath=false` 才能正确取文件名。

---

## 红线 5：语法与格式（阻断级）

```bash
# 每个改动的 .py
toolkit/python.exe -c "import py_compile; py_compile.compile(r'<f>', cfile='<tmp>', doraise=True)"
# 每个改动的 .json
toolkit/python.exe -c "import json; json.load(open(r'<f>', encoding='utf-8'))"
```

**格式规范（本仓库特有）**：

- `module/*/assets.py` 是**一行一个 Button** 的超长单行（数千字符），改它不要重排/换行，否则整个文件变成全量 diff
- `config/template.json` 的值必须是纯标量
- 两个 `.bat` 是 **GBK 编码**，用 UTF-8 读写会乱码
- 生成物（`args.json` / `config_generated.py` / `template.json` / i18n）**只能由生成器改**，不要手改

---

## 红线 6：分支与工作区状态（阻断级）

| 约定 | 要求 |
|---|---|
| `dev` | 主开发线 = 仪表盘 + ModHandler + 上游同步 |
| `ziyong` | **内容必须等于 dev**，同步只做快进：`git push origin dev:ziyong`，**不要 merge** |
| `master` | 仪表盘 + 上游，**不含 ModHandler**；本地平时不存在，需要时 `git switch -c master origin/master`，用完 `git branch -d master` |
| 合并上游 | 在**主工作区**做（HEAD 保持在 dev） |
| 同步 master | **必须用 `git worktree add`**，绝不切主工作区 HEAD |

**`master` 同步必须用 worktree 的原因**：`config/deploy.yaml` 里 `Branch: dev`，GUI 每 5 分钟比对 `HEAD` 与 `origin/dev`。只要 HEAD 短暂离开 dev（哪怕几十秒），检查撞上就会报"有新版可用"，而且**报过之后会锁死不会自愈**。

---

## 红线 7：GUI 更新器相关（阻断级，涉及 `config/deploy.yaml` 时）

| 检查点 | 说明 |
|---|---|
| `AutoUpdate` 必须是 `true` | `_check_update()` **不看**这个开关，但 `git_install()` **看**。若为 `false` 且本地落后 → 点「更新」变成"只重启不更新"的死循环 |
| `Branch` 必须与实际 checkout 一致 | 不一致会永远报"有新版可用"，点更新会 `reset --hard origin/<Branch>` 硬切工作区 |
| `config/deploy.yaml` 不受 git 管理 | 被 `.gitignore:6 config/*.yaml` 忽略（`!config/deploy.*.yaml` 匹配不到单点形式）→ `reset --hard` 冲不掉，但也**不会随仓库同步**，换机器要手工确认 |

---

## 红线 8：判据要用实测，不要推理（方法论级）

三条血泪教训：

1. **断言素材是否重复 → 一律 `git hash-object` 比对**。不要从"上游没删它"反推"它和 cn 不同"。
2. **`git diff <上游> <fork>` 的"新增行" ≠ 本次新增**。fork 独有历史内容会一律显示成"新增"，判断归属要 `git log -S <符号>`。
3. **清理重复素材前先 `git cat-file -e upstream/master:<path>`**：fork 独有 → 可删；上游也有 → 不要删（否则制造分歧 + 未来 modify/delete 冲突）。

---

## 红线 9：提交信息（建议级）

合并提交应写清**同步区间 + 提交数 + 按模块的要点**，而不是 git 默认的 `Merge remote-tracking branch ...`。范例：

```
Merge remote-tracking branch 'upstream/master' into dev

同步上游 56e6ce0b0..cb16b4a89（6 个提交）：
- 战斗 UI：新增 PAUSE_Bullet 暂停按钮（新皮肤），combat.py 与 exercise/hp_daemon.py 同步识别
- 岛屿：慢设备修复 —— 餐厅加载动画检测、生产页完成判定、商店名称 OCR 重试
- 工具：uiautomator2_screenshot.py 改存 webp（101 = 无损）
- 资产：新增 2 个 cn 资产（en/jp/tw 均指向 cn）
```

若有冲突，另起一段写明**冲突文件 + 逐变量裁决**（参见 `conflict-ledger.md` C-001）。

---

## 追加区（新红线请写在这条线以下）
### 2026-10-18 审查回写：红线 3 基线数字更新（实测）

- 实测命令：扫描 `module/**/assets.py` + `campaign/**/assets.py` 全部 `./assets/...` 字符串引用 → `os.path.exists`
- 实测结果：**45 个 assets.py / 7204 条引用 / 断链 0**（旧基线"46 个 / 6220–6228 条"已过期）
- 偏差原因：引用数差约 +976 来自上游合并 `cb16b4a89`（PR #6056）与此前批次新增资产常量（`PAUSE_Bullet`、岛屿 `ISLAND_RESTAURANT_ANIMATION_CHECK` 等）；文件数 45 vs 46 为扫描口径差异（`module/` + `campaign/` 顶层递归，不含 submodule）。**断链 0 的核心判据不受影响**。
- 后续以"断链 0"为阻断判据，文件数/引用数基线以最近一次实测为准，偏差 >5% 时先解释口径再判级。

### 2026-10-18 审查回写：红线 9 提醒（建议级，未修）

- `6879ba03d`（2026-10-17 合并 56e6ce0b0→cb16b4a89）的提交信息是 git 默认的 `Merge remote-tracking branch 'upstream/master' into dev`，**未按红线 9 范例写同步区间 + 提交数 + 按模块要点**。功能无影响，后续合并提交应补上（范例见红线 9）。
