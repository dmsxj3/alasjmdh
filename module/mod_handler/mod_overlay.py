"""
ModOverlay — 悬浮窗直点后端（零重启）。

原理（2026-09-11 在 MuMu + 官服 JMBQ 3.4.0 上实测确认）：
  1. 悬浮窗由注入游戏进程的代码创建，是 WindowManager 的 APPLICATION_OVERLAY 窗口，
     `fl=NOT_FOCUSABLE`（**没有** NOT_TOUCHABLE）⇒ 可以 `input tap` 点它。
  2. mod 里注册了一个平时不被调用的备用 Service（默认 com.android.support.Launcher，
     exported=false）。用 root 执行
        am stopservice -n <pkg>/<svc>  &&  am startservice -n <pkg>/<svc>
     就能让悬浮窗重新出现（Service 还活着时只走 onStartCommand，不会重建窗口，
     所以必须先 stopservice）。
     ★ 约束：`am startservice` 在**游戏处于后台时会失败**（Android 8+ 后台启动限制会报
       `Error: app is in background ...`）。ALAS 跑任务时游戏一定在前台，所以没问题；
       但实现上必须先确认游戏在前台，否则直接走降级。
  3. 悬浮窗存活秒数 = prefs 键 `-98`（mod 的 ShowMenu 里 postDelayed(Menu$1, -98*1000)），
     用户可在 mod 面板里设置，实测为 10。超时后 Menu$1 会 removeView 真杀死窗口
     （不是隐藏）。⇒ 所有点击动作必须在存活期内做完，并且做完要等它消失再继续任务。
  4. 面板「常用」页的实际布局（1280x720 屏，面板矩形 (456,6)-(891,507)，即 435x501）：
        攻击倍率:  1000   [滑块]        <- prefs 键 1
        防御倍率:  1000   [滑块]        <- prefs 键 2
        舰船装填倍率: 1000 [滑块]        <- prefs 键 3
        以德服人          [开关]        <- prefs 键 35
     「倍率」是 3 个滑块，「以德服人」是唯一的开关。

为什么用「点数字 → 输入框」而不是拖滑块（重要）：
  - 滑块只能拖/点，落点必然有误差：实测点滑轨左端得到的是 4 而不是 1，
    而 OffKeys 要求恰好 1 ⇒ 需要「改完再识图确认」，误差还会让状态判定反复失配。
  - 点「攻击倍率: 1000」里那串数字会弹出 AlertDialog：标题 `Input number`、
    一个空的 EditText（hint 是 `Max value: 1000`）、`CANCEL` / `OK`。
    这个对话框**完整地出现在 uiautomator 辅助功能树里**（实测）：
        android:id/alertTitle            text="Input number"
        class=android.widget.EditText    （无 resource-id）
        android:id/button2               text="CANCEL"
        android:id/button1               text="OK"
    ⇒ 可以用 uiautomator2 直接 `set_text('1')` + 点 button1，**精确输入 1，零误差**，
      而且 set_text 走 a11y ACTION_SET_TEXT，不弹输入法、不会把对话框顶移位。
  - mod 在值变化时会立刻把新值写回 prefs XML ⇒ 改完读一次 XML 就能**精确核对**。

因此本后端的做法是：
  调起悬浮窗 → 展开 → 对每个目标键：点数字/点开关 → （数字走输入框）→ OK
  → 读 prefs XML 精确核对（可选再叠加截图识别）→ 等悬浮窗自杀清场。

失败兜底：调不起悬浮窗、模板/坐标不可用、或核对不通过时，按 `OverlayFallbackPrefs`
（默认开）降级到 ModPrefs（写 XML + 重启游戏）。宁可多一次重启，也不能让敏感任务
带着倍率跑 —— 那正是这个功能要防的封号风险。
"""
import os
import re
import time

from module.config.deep import deep_get
from module.logger import logger

try:
    from module.base.base import ModuleBase
except ImportError:  # pragma: no cover - webui 进程 PIL 被换成假模块时的降级
    class ModuleBase:
        def __init__(self, config=None, device=None, task=None):
            self.config = config
            self.device = device

TEMPLATE_DIR = './assets/mod_handler'
TEMPLATES = ['OVERLAY_EXPANDED', 'OVERLAY_COLLAPSED', 'SWITCH_ON', 'SWITCH_OFF']

# 面板矩形内的相对坐标（比例；来自 1280x720 实测，(456,6)-(891,507) = 435x501）。
# 用比例而不是像素，是为了在密度/分辨率变化时仍然对得上（面板是固定 dp 布局）。
REL_NUMBER = [(0.291, 0.256), (0.291, 0.427), (0.291, 0.600)]  # 攻击/防御/装填倍率 的数字
REL_TOGGLE = (0.940, 0.757)                                    # 以德服人 开关
EXPANDED_MIN_RATIO = 0.15   # 面板宽度 > 屏宽 15% 即认为已展开（收起小球仅约 2%）

# 键位语义（写在 OffKeys/OnKeys 里的数字 ID）；本后端按「整型键升序 = 三个滑块」
# 「布尔键 = 常用页唯一的开关」来映射，与 mod 实际布局一致。
_warn_missing_tpl = False


def _safe_int(text, default=None):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


class ModOverlay(ModuleBase):
    def __init__(self, config=None, device=None):
        super().__init__(config=config, device=device)
        self.config = config
        self.device = device
        self._prefs = None
        self._tpl = None
        self._u2 = None
        self._screen = None
        # 一旦检测到「调起悬浮窗把游戏搞崩（pid 变化/消失）」，本进程内永久停用 overlay，
        # 后续全部走 prefs 降级 —— 绝不在崩溃上重试，避免把游戏打进崩溃循环。
        self._disabled = False

    # ------------------------------------------------------------ 配置
    @property
    def package(self):
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.PackageName',
                            default='com.bilibili.azurlane') or 'com.bilibili.azurlane')

    @property
    def service(self):
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.OverlayService',
                            default='com.android.support.Launcher')
                   or 'com.android.support.Launcher')

    @property
    def survival_seconds(self):
        """面板存活秒数。优先取设备上真实的 -98，取不到才用配置值。"""
        sec = self._prefs_int('-98')
        if sec is None:
            sec = _safe_int(deep_get(self.config.data,
                                     'ModHandler.ModHandler.OverlaySurvivalSeconds',
                                     default=20), 20)
        return max(5, min(int(sec or 20), 120))

    @property
    def fallback_enabled(self):
        return bool(deep_get(self.config.data,
                             'ModHandler.ModHandler.OverlayFallbackPrefs',
                             default=True))

    @property
    def visual_verify(self):
        return bool(deep_get(self.config.data,
                             'ModHandler.ModHandler.OverlayVisualVerify',
                             default=True))

    # ------------------------------------------------------------ 依赖对象
    @property
    def prefs_reader(self):
        """复用 ModPrefs 读/写 XML（mod 点击后自己会同步落盘，所以 XML = 内存状态）。"""
        if self._prefs is None:
            from module.mod_handler.mod_prefs import ModPrefs
            self._prefs = ModPrefs(config=self.config, device=self.device)
        return self._prefs

    @property
    def u2(self):
        """仅备用：热路径里不要用 —— u2 冷启动会拉起 atx/uiautomator 服务，动辄十几秒，
        而面板只活 -98 秒。留给人工排查用。"""
        if self._u2 is None:
            import uiautomator2 as u2
            self._u2 = u2.connect(self.device.serial)
            self._u2.wait_timeout = 5.0
        return self._u2

    def _write_survival(self):
        """
        把 prefs 的 -98（悬浮窗存活秒数）抬到配置值，让面板有更充裕的操作时间。

        ★ 只能在**游戏没运行时**写：游戏运行中改文件会被进程退出时的回写覆盖掉，
        而且我们不想为了这一项就把游戏停掉（那就成了"多余的重启"）。
        所以这是"尽力而为"：等哪天游戏恰好是关着的（比如 ALAS 停在这台机器上），
        下次启动就生效。mod 面板里该项上限 20，实测用户设的是 10。
        """
        sec = _safe_int(deep_get(self.config.data,
                                 'ModHandler.ModHandler.OverlaySurvivalSeconds',
                                 default=20), 20)
        if not sec or sec < 5:
            return False
        try:
            if str(self.device.adb_shell(['pidof', self.package]) or '').strip():
                return False   # 游戏在跑，写了也会被覆盖，跳过
            raw = self.prefs_reader.read_raw()
            if not raw:
                return False
            new, n = re.subn(r'(<int name="-98" value=")\d+(")', rf'\g<1>{int(sec)}\g<2>', raw)
            if n == 0 or new == raw:
                return False
            self.prefs_reader.write_raw(new)
            logger.info(f'ModOverlay: prefs -98 已设为 {int(sec)}s（下次游戏启动生效）')
            return True
        except Exception as e:
            logger.info(f'ModOverlay: 提升存活秒数跳过（{type(e).__name__}: {e}）')
            return False

    def _prefs_raw(self):
        try:
            raw = self.prefs_reader.read_raw()
        except Exception as e:
            logger.warning(f'ModOverlay: read prefs failed: {type(e).__name__}: {e}')
            return None
        if not raw:
            return None
        try:
            return self.prefs_reader.parse(raw)
        except Exception as e:
            logger.warning(f'ModOverlay: parse prefs failed: {e}')
            return None

    def _prefs_int(self, key):
        parsed = self._prefs_raw()
        if not parsed:
            return None
        cur = parsed.get(str(key))
        return _safe_int(cur[1]) if cur else None

    # ------------------------------------------------------------ 目标值 / 状态
    def _target(self, mode):
        """目标键值对：mode=True 取 OnKeys，否则取 OffKeys。"""
        from module.mod_handler.mod_handler import parse_key_values
        keys = (deep_get(self.config.data, 'ModHandler.ModHandler.OnKeys', default='')
                if mode else
                deep_get(self.config.data, 'ModHandler.ModHandler.OffKeys', default=''))
        return parse_key_values(keys) or {}

    @staticmethod
    def _at_target(parsed, target, tol_int=0):
        """
        parsed（prefs 解析结果）是否已经等于 target。
        整型允许 tol_int 的容差：滑块 UI 可能有最小步进，写回去不一定正好等于配置值。
        """
        for k, want in target.items():
            cur = parsed.get(str(k))
            if cur is None:
                return False
            value = str(cur[1])
            if isinstance(want, bool):
                if (value.lower() == 'true') != want:
                    return False
            else:
                got = _safe_int(value)
                if got is None:
                    return False
                if tol_int and abs(got - int(want)) > tol_int:
                    return False
                if not tol_int and got != int(want):
                    return False
        return True

    def _tolerance(self, target):
        """整型容差：按目标值量级给 1%。倍率 1 时容差为 0（必须真的是 1）。"""
        vals = [int(v) for v in target.values() if not isinstance(v, bool)]
        if not vals:
            return 0
        return 0 if min(vals) <= 10 else max(1, min(vals) // 100)

    def get_state(self):
        """
        读设备真实状态。
        True = 已处于 OnKeys 描述的状态；False = 已处于 OffKeys 描述的状态；None = 都不是。
        """
        parsed = self._prefs_raw()
        if not parsed:
            return None
        off, on = self._target(False), self._target(True)
        if off and self._at_target(parsed, off, tol_int=self._tolerance(off)):
            return False
        if on and self._at_target(parsed, on, tol_int=self._tolerance(on)):
            return True
        return None

    def describe_state(self):
        off, on = self._target(False), self._target(True)
        if not off and not on:
            return {'option': 'unconfigured',
                    'detail': 'OffKeys / OnKeys 都为空，无法判定状态'}
        parsed = self._prefs_raw()
        if not parsed:
            return {'option': 'unknown', 'detail': f'读不到 {self.prefs_reader.remote_path}（需要 root）'}
        state = self.get_state()
        if state is True:
            return {'option': 'on', 'detail': '倍率已开启（读悬浮窗 prefs XML）'}
        if state is False:
            return {'option': 'off', 'detail': '倍率已关闭（读悬浮窗 prefs XML）'}
        detail = '  '.join(f'{k}={parsed.get(str(k), ("", "缺失"))[1]}' for k in
                           list(off) + [k for k in on if k not in off])
        return {'option': 'unknown', 'detail': f'不在任一目标状态上: {detail}'}

    # ------------------------------------------------------------ 窗口几何
    def _screen_size(self):
        if self._screen is None:
            try:
                out = str(self.device.adb_shell(['wm', 'size']) or '')
                m = re.search(r'(\d+)x(\d+)', out)
                self._screen = (int(m.group(1)), int(m.group(2))) if m else (1280, 720)
            except Exception:
                self._screen = (1280, 720)
        return self._screen

    def overlay_frame(self):
        """
        取悬浮窗矩形 (x0, y0, w, h)，取不到返回 None。

        判据：同一窗口块里同时出现游戏包名、APPLICATION_OVERLAY、gr=TOP*（悬浮窗是
        TOP|LEFT|CENTER 对齐；mod 的 AlertDialog 是 gr=CENTER，靠这个区分）。
        APPLICATION_OVERLAY 只由 overlay 权限的窗口使用，不会误伤普通 Activity。
        """
        try:
            out = str(self.device.adb_shell(['dumpsys', 'window', 'windows'], timeout=40) or '')
        except Exception as e:
            logger.warning(f'ModOverlay: dumpsys window failed: {e}')
            return None
        block = []
        for line in out.splitlines():
            if line.strip().startswith('Window #'):
                block = [line]
                continue
            if not block:
                continue
            block.append(line)
            if 'mFrame=' not in line:
                continue
            text = '\n'.join(block)
            block = []
            if self.package not in text or 'APPLICATION_OVERLAY' not in text:
                continue
            if not re.search(r'gr=\s*TOP', text):
                continue
            m = re.search(r'mFrame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', line)
            if not m:
                continue
            x0, y0, x1, y1 = (int(v) for v in m.groups())
            if x1 <= x0 or y1 <= y0:
                continue
            return x0, y0, x1 - x0, y1 - y0
        return None

    def _expanded(self, frame):
        if not frame:
            return False
        w, _ = self._screen_size()
        return frame[2] >= EXPANDED_MIN_RATIO * w

    def _is_foreground(self):
        try:
            out = str(self.device.adb_shell(['dumpsys', 'window'], timeout=30) or '')
            m = re.search(r'mCurrentFocus=Window\{[^}]*\s([\w.]+/[\w.$]+)\}', out)
            return bool(m) and m.group(1).startswith(self.package)
        except Exception:
            return False

    # ------------------------------------------------------------ 动作
    def _tap(self, x, y):
        self.device.adb_shell(['input', 'tap', str(int(x)), str(int(y))])

    def _game_pid(self):
        try:
            return str(self.device.adb_shell(['pidof', self.package]) or '').strip()
        except Exception:
            return ''

    def _game_running(self):
        return bool(self._game_pid())

    def show(self, timeout=10):
        """
        调起悬浮窗（先停后启 Service），返回 True/False。

        ★★ 安全前提（2026-09-11 血泪教训）★★
        游戏进程**必须已经在跑**，否则直接放弃、交给 prefs 降级。
        原因：`am startservice` 在进程不存在时会**新拉起一个进程**来跑这个 Service，
        而那个新进程里游戏的原生库还没初始化 —— 实测 `Launcher.onCreate -> new Menu
        -> Menu.Icon()` 直接抛 `UnsatisfiedLinkError: No implementation found for
        Java_com_android_support_Menu_Icon`，**把整个游戏进程搞崩**
        （logcat: am_crash + am_proc_died，任务随后 GameNotRunningError）。
        所以这里宁可调不起悬浮窗，也绝不冒"为了省一次重启把游戏搞崩"的风险。
        """
        if self._disabled:
            return False
        if not self._game_running():
            logger.warning('ModOverlay: 游戏进程不在运行 —— 此时 startservice 会新起进程并把游戏搞崩，'
                           '跳过悬浮窗、直接降级')
            return False

        target = f'{self.package}/{self.service}'
        if not self._is_foreground():
            # 后台时 Android 会拒绝启动 Service（实测报 "Error: app is in background ..."）。
            # 但前台判定本身也可能因为转场/浮层而误判，所以只提示、不直接放弃，
            # 真正的结论由后面「窗口有没有出现」来定。
            logger.warning('ModOverlay: 当前焦点不在游戏上，am startservice 可能被系统拒绝')

        def _run(cmd):
            try:
                self.device.adb_shell(cmd)
                return True
            except Exception:
                try:
                    self.device.adb_shell('su -c "' + ' '.join(cmd) + '"')
                    return True
                except Exception as e:
                    logger.warning(f'ModOverlay: `{" ".join(cmd)}` failed: {e}')
                    return False

        _run(['am', 'stopservice', '-n', target])
        time.sleep(0.6)
        out = ''
        try:
            out = str(self.device.adb_shell(['am', 'startservice', '-n', target]) or '')
        except Exception:
            try:
                out = str(self.device.adb_shell(f'su -c "am startservice -n {target}"') or '')
            except Exception as e:
                logger.warning(f'ModOverlay: startservice failed: {e}')
        if 'Error' in out:
            logger.warning(f'ModOverlay: startservice 报错: {out.strip()}')

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.overlay_frame():
                return True
            time.sleep(0.4)
        logger.warning('ModOverlay: 悬浮窗在 %ss 内没有出现' % timeout)
        return False

    def expand(self, frame=None, timeout=6):
        """收起态则点小球展开，返回展开后的 frame（失败返回 None）。"""
        frame = frame or self.overlay_frame()
        if not frame:
            return None
        if self._expanded(frame):
            return frame
        x0, y0, w, h = frame
        self._tap(x0 + w // 2, y0 + h // 2)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            cur = self.overlay_frame()
            if cur and self._expanded(cur):
                return cur
        logger.warning('ModOverlay: 悬浮窗没能展开')
        return None

    @staticmethod
    def _row_point(frame, rel):
        x0, y0, w, h = frame
        return int(x0 + rel[0] * w), int(y0 + rel[1] * h)

    def _dialog_frame(self):
        """
        mod 的 AlertDialog 也是游戏进程的 APPLICATION_OVERLAY 窗口，但对齐是 gr=CENTER
        （悬浮窗面板是 gr=TOP）—— 用这个把它们区分开。
        """
        try:
            out = str(self.device.adb_shell(['dumpsys', 'window', 'windows'], timeout=40) or '')
        except Exception:
            return None
        block = []
        for line in out.splitlines():
            if line.strip().startswith('Window #'):
                block = [line]
                continue
            if not block:
                continue
            block.append(line)
            if 'mFrame=' not in line:
                continue
            text = '\n'.join(block)
            block = []
            if self.package not in text or 'APPLICATION_OVERLAY' not in text:
                continue
            if not re.search(r'gr=\s*CENTER', text):
                continue
            m = re.search(r'mFrame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', line)
            if not m:
                continue
            x0, y0, x1, y1 = (int(v) for v in m.groups())
            if x1 > x0 and y1 > y0:
                return x0, y0, x1 - x0, y1 - y0
        return None

    def _ok_point(self, dialog):
        """
        OK 按钮在对话框内的相对位置（实测 1280x720：对话框 (288,224)-(991,496)=703x272，
        OK 中心 (901,430) ⇒ (0.872, 0.757)；CANCEL 中心 (795,430) ⇒ (0.721, 0.757)）。
        用途：uiautomator2 有时查不到 button1（实测会漏），用 frame 算坐标更稳。
        """
        x0, y0, w, h = dialog
        return int(x0 + 0.872 * w), int(y0 + 0.757 * h)

    def _cancel_point(self, dialog):
        x0, y0, w, h = dialog
        return int(x0 + 0.721 * w), int(y0 + 0.757 * h)

    def _dismiss_dialog(self):
        dlg = self._dialog_frame()
        if not dlg:
            return
        cx, cy = self._cancel_point(dlg)
        self._tap(cx, cy)
        time.sleep(0.5)

    def _set_number(self, frame, rel, value, timeout=6):
        """
        点开数字输入框，精确写入 value。

        ★ 这里刻意**不用 uiautomator2**：u2 首次查询要先把 atx/uiautomator 服务拉起来，
        实测冷启动能吃掉 10~40 秒，而面板的存活期只有 -98 秒（用户设的 10 秒），
        等 u2 就绪时面板早没了。所以全部走 adb：`input tap` + `input text`，
        位置由 dumpsys 给的对话框矩形换算。
        """
        x, y = self._row_point(frame, rel)
        self._tap(x, y)
        dlg = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            dlg = self._dialog_frame()
            if dlg:
                break
            time.sleep(0.25)
        if not dlg:
            logger.warning('ModOverlay: 数字输入框没有弹出（面板是否已过 -98 存活期？）')
            return False

        # 点一下输入框保证它有焦点，再输入。输入法弹出时对话框会按 adjust=pan 上移，
        # 所以输入完要重新读一次矩形再点 OK。
        self._tap(dlg[0] + int(0.45 * dlg[2]), dlg[1] + int(0.46 * dlg[3]))
        time.sleep(0.3)
        self.device.adb_shell(['input', 'text', str(value)])
        time.sleep(0.3)

        dlg = self._dialog_frame() or dlg
        ox, oy = self._ok_point(dlg)
        self._tap(ox, oy)
        deadline = time.time() + 4
        while time.time() < deadline:
            if not self._dialog_frame():
                return True
            time.sleep(0.25)
        logger.warning('ModOverlay: 输入框没有关闭，取消它')
        self._dismiss_dialog()
        return False

    def _toggle(self, frame, rel):
        x, y = self._row_point(frame, rel)
        self._tap(x, y)
        return True

    # ------------------------------------------------------------ 校验
    def _verify_prefs(self, target):
        """读 XML 精确核对（mod 自己写盘，等价于它的内存状态）。"""
        parsed = self._prefs_raw()
        if not parsed:
            return None
        ok = self._at_target(parsed, target, tol_int=self._tolerance(target))
        detail = '  '.join(
            f'{k}={parsed.get(str(k), ("", "缺失"))[1]}→目标'
            f'{("true" if v else "false") if isinstance(v, bool) else v}'
            for k, v in target.items())
        logger.attr('ModOverlay.verify(prefs)', f'{"OK" if ok else "MISMATCH"} {detail}')
        return ok

    def _verify_visual(self, frame, target):
        """
        截图识别复核（尽力而为）：
          - 「以德服人」用模板匹配确认开关是 开/关 的图形；
          - 三个倍率用 OCR 读数字并和目标值比对。
        读不出来（模板缺失 / OCR 失败）返回 None，不据此判定失败 ——
        权威判据是 prefs XML（mod 自己写的），截图只作为第二道确认。
        """
        tpls = self.templates()
        if tpls is None:
            return None
        try:
            image = self.device.screenshot()
        except Exception as e:
            logger.warning(f'ModOverlay: screenshot failed: {e}')
            return None
        result = {}
        try:
            bool_keys = [k for k, v in target.items() if isinstance(v, bool)]
            if bool_keys and 'SWITCH_ON' in tpls:
                want_on = bool(target[bool_keys[0]])
                name = 'SWITCH_ON' if want_on else 'SWITCH_OFF'
                result['toggle'] = bool(tpls[name].match(image))
        except Exception as e:
            logger.warning(f'ModOverlay: 开关识图失败: {type(e).__name__}: {e}')
        try:
            int_keys = sorted(k for k, v in target.items() if not isinstance(v, bool))
            if int_keys:
                from module.base.button import Button
                from module.ocr.ocr import Digit
                values = {}
                for i, k in enumerate(int_keys[:len(REL_NUMBER)]):
                    x, y = self._row_point(frame, REL_NUMBER[i])
                    area = (x - 40, y - 18, x + 60, y + 18)
                    button = Button(area=area, color=(), button=(0, 0, 0, 0))
                    ocr = Digit(button, letter=(0, 255, 0), threshold=160)
                    got = ocr.ocr(image)
                    values[str(k)] = got
                result['numbers'] = values
        except Exception as e:
            logger.info(f'ModOverlay: 数字识图跳过（{type(e).__name__}: {e}）')
        logger.attr('ModOverlay.verify(visual)', result if result else 'unavailable')
        return result or None

    def templates(self):
        global _warn_missing_tpl
        if self._tpl is not None:
            return self._tpl
        result = {}
        for name in TEMPLATES:
            path = os.path.join(TEMPLATE_DIR, f'{name}.png')
            if not os.path.exists(path):
                if not _warn_missing_tpl:
                    logger.info(f'ModOverlay: 模板 {path} 不存在，跳过截图复核'
                                f'（不影响控制，权威判据是 prefs XML）')
                _warn_missing_tpl = True
                return None
            try:
                from module.base.template import Template
                result[name] = Template(file=path)
            except Exception as e:
                logger.warning(f'ModOverlay: 载入模板 {path} 失败: {e}')
                return None
        self._tpl = result
        return result

    # ------------------------------------------------------------ 清场
    def wait_gone(self, extra=4, cap=90):
        """
        等悬浮窗被 mod 自己杀死（removeView）后再返回，避免面板留在屏幕上干扰
        Alas 后续的识图。先按设备真实 -98 估时限，轮询窗口列表，一消失就立刻返回。
        """
        deadline = time.time() + min(cap, self.survival_seconds + extra)
        while time.time() < deadline:
            if not self.overlay_frame():
                logger.attr('ModOverlay', 'overlay gone, screen clean')
                return True
            time.sleep(0.4)
        # 兜底收尾：主动把面板关掉（点「关闭窗口」所在的底行右侧）
        frame = self.overlay_frame()
        if frame:
            x0, y0, w, h = frame
            self._tap(x0 + int(0.82 * w), y0 + int(0.93 * h))
            time.sleep(1.0)
        still = bool(self.overlay_frame())
        if still:
            logger.warning('ModOverlay: 悬浮窗仍在屏幕上，后续识图可能受干扰')
        return not still

    # ------------------------------------------------------------ 主入口
    def set_multiplier(self, mode: bool, restart=None):
        """
        把倍率切到目标状态，全程不重启游戏。

        Args:
            mode: True = 开倍率（OnKeys），False = 关倍率（OffKeys）
            restart: 兼容签名，overlay 后端不使用
        Returns:
            bool: 是否确认达成目标状态
        """
        target = self._target(mode)
        if not target:
            logger.warning(f'ModOverlay: {"OnKeys" if mode else "OffKeys"} 为空，无从下手')
            return False

        state = self.get_state()
        if state == mode:
            logger.attr('ModOverlay', f'multiplier already {"ON" if mode else "OFF"}')
            return True
        if state is None and self._at_target(self._prefs_raw() or {}, target,
                                            tol_int=self._tolerance(target)):
            return True

        # 尽力而为：游戏恰好关着就把 -98 抬到配置值，下次启动后面板操作时间更充裕。
        try:
            self._write_survival()
        except Exception:
            pass

        int_keys = sorted(k for k, v in target.items() if not isinstance(v, bool))
        bool_keys = [k for k, v in target.items() if isinstance(v, bool)]

        ok = False
        if self._disabled:
            logger.warning('ModOverlay: overlay 后端已在本进程内被停用（此前把游戏搞崩过），'
                           '直接走 prefs 降级')
        else:
            try:
                ok = self._apply(target, int_keys, bool_keys, mode)
            except Exception as e:
                logger.error(f'ModOverlay: 悬浮窗操作异常: {type(e).__name__}: {e}')
                ok = False
            finally:
                self.wait_gone()

        if ok:
            parsed = self._prefs_raw() or {}
            if not self._at_target(parsed, target, tol_int=self._tolerance(target)):
                logger.error('ModOverlay: prefs 复核不通过，按失败处理')
                ok = False

        if not ok:
            logger.warning('ModOverlay: 悬浮窗方式未能确认生效')
            if self.fallback_enabled:
                logger.warning('ModOverlay: 降级到 prefs（需要重启游戏才生效）')
                try:
                    from module.mod_handler.mod_prefs import ModPrefs
                    changed = ModPrefs(config=self.config, device=self.device).set_multiplier(mode)
                    return bool(changed)
                except Exception as e:
                    logger.error(f'ModOverlay: prefs 降级也失败: {type(e).__name__}: {e}')
            else:
                logger.critical('ModOverlay: 未生效且已关闭降级，'
                                '敏感任务可能带着倍率开打，请立刻检查！')
            return False
        return True

    def _apply(self, target, int_keys, bool_keys, mode):
        """
        逐个键改，**每次调起悬浮窗只做一个键**。

        为什么不做「一次调起全套改完」：面板存活期就是 prefs 的 -98 秒（用户设的 10 秒，
        mod 面板里上限也只有 20），而一次「调起 + 展开 + 点数字 + 输入 + OK」实测要
        2~4 秒，四个键塞进去必然中途被 mod 自己杀掉。所以这里改成一次一个键、
        自动重试收敛；每次都会等面板消失再继续，绝不会把面板留在屏幕上。
        """
        limit = max(4, len(int_keys) + len(bool_keys) + 2)
        attempts = 0
        while attempts < limit and not self._disabled:
            attempts += 1
            pid_before = self._game_pid()
            changed = self._prefs_raw() or {}
            todo_int = [k for k in int_keys
                        if not self._at_target(changed, {k: target[k]},
                                               tol_int=self._tolerance({k: target[k]}))]
            todo_bool = [k for k in bool_keys
                         if not self._at_target(changed, {k: target[k]})]
            if not todo_int and not todo_bool:
                return True

            if not self.show():
                return False
            frame = self.expand()
            if not frame:
                self.wait_gone()
                continue

            did = False
            if todo_int:
                key = todo_int[0]
                rel = REL_NUMBER[min(len(REL_NUMBER) - 1, sorted(int_keys).index(key))]
                logger.info(f'ModOverlay: 键 {key} -> {target[key]}（数字输入）')
                did = self._set_number(frame, rel, target[key])
            else:
                key = todo_bool[0]
                logger.info(f'ModOverlay: 键 {key} -> {target[key]}（点开关）')
                did = self._toggle(frame, REL_TOGGLE)
                time.sleep(0.5)

            # 崩溃检测：正常操作前后游戏 pid 必须一致。变了/没了说明我们把游戏搞崩了
            # （startservice 新起进程 -> Launcher.onCreate -> Menu.Icon() UnsatisfiedLinkError），
            # 立刻永久停用 overlay，绝不重试。
            pid_after = self._game_pid()
            if not pid_after or pid_after != pid_before:
                self._disabled = True
                logger.critical(
                    f'ModOverlay: 调起悬浮窗后游戏进程消失/重启（pid {pid_before!r} -> {pid_after!r}）。'
                    f'本进程内永久停用 overlay 后端，改用 prefs 降级。'
                    f'根因通常是 Mod 的 x86_64 原生库没有实现 Menu.Icon()：'
                    f'进程不在时 startservice 会新起进程，Launcher.onCreate -> new Menu -> Menu.Icon() '
                    f'抛 UnsatisfiedLinkError，整个游戏进程崩溃。')
                return False

            # 所有目标键都达成时，趁面板还在屏幕上做一次截图复核
            # （模板比对「以德服人」开关图形 + OCR 读三个倍率数字）。
            # 权威判据始终是 prefs XML；截图读不出来只记日志，不判失败。
            changed = self._prefs_raw() or {}
            if self.visual_verify and self._at_target(changed, target,
                                                      tol_int=self._tolerance(target)):
                try:
                    self._verify_visual(frame, target)
                except Exception as e:
                    logger.info(f'ModOverlay: 截图复核跳过（{type(e).__name__}: {e}）')

            self.wait_gone()
            if not did:
                logger.warning(f'ModOverlay: 键 {key} 本次未成功，重试（第 {attempts} 次）')
        return False

    def repair(self, mode: bool, restart=None):
        """
        overlay 后端没有「部分匹配」概念：要么整组键一起改，要么不动。
        这里恒返回 False，让 ModHandler 走完整流程（含敏感任务日志与校验）。
        """
        return False

    def verify_applied(self, mode: bool):
        return self.get_state() is mode

    def diagnose(self):
        info = {
            'backend': 'overlay',
            'overlay_service': self.service,
            'survival_seconds': self.survival_seconds,
            'fallback_to_prefs': self.fallback_enabled,
            'game_foreground': None,
            'overlay_frame': None,
            'prefs_state': None,
            'templates': {name: os.path.exists(os.path.join(TEMPLATE_DIR, f'{name}.png'))
                          for name in TEMPLATES},
        }
        try:
            info['game_foreground'] = self._is_foreground()
        except Exception as e:
            info['game_foreground'] = f'error: {e}'
        try:
            info['overlay_frame'] = self.overlay_frame()
        except Exception as e:
            info['overlay_frame'] = f'error: {e}'
        try:
            info['prefs_state'] = self.describe_state()
        except Exception as e:
            info['prefs_state'] = f'error: {e}'
        return info
