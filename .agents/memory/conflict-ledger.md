# 历史冲突判例库（conflict-ledger）

> 格式：`冲突文件/模块 → 冲突原因 → 标准解决范式 / 保留逻辑`
> 每条判例都来自本仓库真实发生过的合并，可直接作为下次的裁决依据。
> 新增判例请**追加**到文末，不要覆盖历史。

---

## C-001 `module/campaign/assets.py` —— 上游素材路径改写 vs Fork 独有 Button（★最高频）

**发生时间**：2026-10-16，合并 `7d6a415e3 → 56e6ce0b0`（上游 PR #6026）
**合并提交**：`6c6f8fe90`（dev）、`8a1f120e0`（master，另一 Agent 独立解决，结果与 dev **逐字节相同**）

### 冲突原因

上游 `3ec2d33c8 Remove en/jp/tw assets identical to cn (#6026)` 删除了 **1741 个**与 cn 逐字节相同的 en/jp/tw 素材，并把 45 个 `module/*/assets.py` 里 `file={...}` 的 en/jp/tw 路径统一改指 `./assets/cn/`。

Fork 侧同一文件里存在 Zuosizhu 2023 年引入的三个 Button：
`OCR_COIN_LIMIT`（`5aa1e7d00`）、`OCR_OIL_LIMIT`（`5aa1e7d00`）、`OCR_GEM`（`7b45936be`）。

上游改写的是 `OCR_COIN` / `OCR_EVENT_PT` / `OCR_OIL` / `OCR_OIL_CHECK` 四行，fork 的三个 Button 恰好插在这几行**中间** → 两类改动挤在同一段相邻行，git 无法自动拼接，报 `CONFLICT (content)`。

### 标准解决范式（逐变量判定，禁止整块选边）

```
对冲突块里的每一行，按「变量名」而不是「位置」决定：

  变量属于 Fork 独有（不在上游文件里）        → 保留 ours
  变量是上游本次改写的既有 Button             → 采用 theirs（改指 cn）
```

本判例的裁决：

| 变量 | 采用 | 理由 |
|---|---|---|
| `OCR_COIN` | theirs | 上游本次改写对象 |
| `OCR_EVENT_PT` | theirs | 同上 |
| `OCR_OIL` | theirs | 同上 |
| `OCR_OIL_CHECK` | theirs | 同上 |
| `OCR_COIN_LIMIT` | **ours** | Fork 独有（Zuosizhu 引入） |
| `OCR_GEM` | **ours** | Fork 独有 |
| `OCR_OIL_LIMIT` | **ours** | Fork 独有 |

### 可复用的实现脚本思路

按变量名（`^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(Button|Template)\(`）建索引，ours 逐行遍历：
名字在 `FORK_NEW` 集合里 → 保留 ours；否则查 theirs 的同名行替换。冲突块处理后校验「剩余冲突标记数 == 0」。

### ⚠️ 当时踩过的认知错误（务必避免重犯）

1. **不要靠推理断言"素材是否相同"**。当时凭"上游没删它 → 说明它和 cn 不同"推断 `OCR_*_LIMIT` 各服素材不同，**实测 blob 哈希后四服完全相同**（`OCR_COIN_LIMIT` = `7c9e29b7…`，`OCR_OIL_LIMIT` = `9392769f…`）。真正原因是：上游 #6026 只处理**它自己仓库里存在**的文件，fork 独有文件本来就不在删除清单里，与"是否相同"无关。
2. **`git diff <上游> <fork>` 里的"新增行"≠ 本次新增**。Zuosizhu 2023 年的 Button 在与上游对比时一律显示成"新增"。判断某行是否本次所改必须 `git log -S <符号>` 溯源作者与日期。

---

## C-002 上游批量删除素材（#6026 删 1741 个）—— 断链风险

**发生时间**：2026-10-16
**性质**：不是文本冲突，但比冲突更危险 —— 合并能过、`py_compile` 能过，**运行时才炸**。

### 风险原因

`assets.py` 里 `file={'cn': './assets/xx/YY.png', ...}` 是**字符串路径**，不是 import。文件被上游删掉后，Python 不会报错，只有 ALAS 跑到那个功能、去 `cv2.imread` 时才抛异常。

### 标准解决范式（每次上游动 assets 必做）

```python
# 扫描 module/ + campaign/ 下全部 assets.py，抽出 ./assets/... 引用逐一验证存在
PATH_RE = re.compile(r"\./assets/[A-Za-z0-9_./\-]+\.(?:png|jpg|jpeg)")
# 对每个匹配：os.path.exists(os.path.join(ROOT, m[2:]))
```

**基线数字**（用于对比是否异常）：

| 时间 | assets.py 数 | 引用条数 | 断链 |
|---|---|---|---|
| 2026-10-16（#6026 后） | 46 | 6220 | 0 |
| 2026-10-17 | 46 | 6228 | 0 |

引用条数增加通常来自新增 Button（每个 Button × 4 服）。

### 补充：`modify/delete` 冲突的预判

合并前先算交集，避免出现"fork 改了、上游删了"的 modify/delete 冲突：

```bash
git diff --name-status <上次同步点> upstream/master | grep '^D' | awk '{print $2}' | sort > /tmp/updel.txt
grep '^assets/' /tmp/fork_files.txt | sort > /tmp/fork_assets.txt
comm -12 /tmp/updel.txt /tmp/fork_assets.txt   # 非空 = 会 modify/delete 冲突
```

2026-10-16 实测交集为空（fork 改的是 `OCR_*_LIMIT`，上游删的是 `OCR_COIN`/`OCR_OIL`/`OCR_OIL_CHECK`）。

---

## C-003 重复素材清理 —— 能不能删的判据（非冲突，但同类裁决）

**发生时间**：2026-10-16（dev `68ccc8fb2`）、2026-10-16（master `1f8fe2e56`）

### 判据（**唯一标准**）

```bash
git cat-file -e upstream/master:<path>
```

- **上游不存在（fork 独有）→ 可删**：删了不会造成与上游的分歧
- **上游也存在 → 不要删**：删了会让 fork 与上游分歧，上游日后改动这些文件时产生 `modify/delete` 冲突，而收益只有几十 KB

### 本判例

- `assets/{en,jp,tw}/campaign/{OCR_COIN_LIMIT,OCR_OIL_LIMIT}.png` 共 6 个：与 cn 逐字节相同、**上游不存在** → 已删，路径改指 `./assets/cn/`（约 40KB）
- 全仓库仍有 **56 个**（master 侧 62 个）与 cn 逐字节相同的重复素材（多为 `*.BUTTON.png`），但**它们上游也存在** → **明确不删**

---

## C-004 `module/campaign/run.py` —— 曾自动合并成功（参考判例）

**发生时间**：2026-10-16，同一批次合并
**结论**：与 `module/campaign/assets.py` 同批冲突候选，但 git 自动合并成功，无需人工介入。
**启示**：`comm -12` 算出的"冲突候选"只是**候选**，实际是否冲突要看 `git merge-tree --write-tree` 的退出码。不要对候选文件预先动手。

---

## C-005 零冲突批次（用于判断"这次是不是异常"）

以下批次的 `git merge-tree --write-tree` 全部 **exit=0、零冲突**：

| 日期 | 区间 | 提交数 | 文件数 | dev 合并提交 |
|---|---|---|---|---|
| 2026-10-08 | `c0476fb4e → 0078abb84` | 21 | 104 | `81bc7f7be` |
| 2026-10-10 | `0078abb84 → 7d6a415e3` | 4 | 7 | `c8e9f93f9` |
| 2026-10-17 | `56e6ce0b0 → cb16b4a89` | 6 | 11 | `6879ba03d` |
| 2026-10-23 | `cb16b4a89 → 4d88c22d8` | 29 | 61 | `e6dda2ca2` |

**启示**：这个 fork 大多数批次是零冲突的。**只有上游大改 `assets.py` 路径或重构目录结构时才容易撞**。如果某次突然出现多个冲突文件，先怀疑"上游做了结构性重构"，而不是"自己改错了"。

### 2026-10-23 批次补充（重要参考）

- 冲突候选 **7 个**（`config/template.json`、`module/campaign/run.py`、`module/config/argument/args.json`、4 个 i18n），**实际冲突 0 个**
- 这 7 个全在「配置五源 + `campaign/run.py`」区，属**高危耦合区**，但两边改的是**不同区域**（上游改活动列表轮换，Fork 改 ModHandler/Dashboard 组）→ git 自动合并成功
- **生成器幂等验证是关键**：合并后跑 `config_updater`，`git status` 为空 → 证明自动合并后的五源仍然自洽
- 教训：**"候选多"不等于"冲突多"**。别因为候选里有高危文件就提前手工介入（`conflict-ledger.md` C-004 已经说过一次，这里再次印证）

---

## 追加区（新判例请写在这条线以下）

<!-- 格式：
## C-00X `<文件路径>` —— <一句话概括>
**发生时间**：YYYY-MM-DD，合并 `<base> → <head>`
**冲突原因**：…
**标准解决范式**：…
-->
