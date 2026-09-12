"""
ModOverlay — 悬浮窗直点后端（零重启）。

原理（2026-09-11 在 MuMu + 官服 JMBQ 3.4.0 上实测确认）：
  1. 悬浮窗由注入游戏进程的代码创建，是 WindowManager 的 APPLICATION_OVERLAY 窗口，
     `fl=NOT_FOCUSABLE`（**没有** NOT_TOUCHABLE）⇒ 可以被点击。
  2. mod 里注册了一个平时不被调用的备用 Service（默认 com.android.support.Launcher，
     exported=false）。用 root 执行
        am stopservice -n <pkg>/<svc>  &&  am startservice -n <pkg>/<svc>
     就能让悬浮窗重新出现（Service 还活着时只走 onStartCommand，不会重建窗口，
     所以必须先 stopservice）。
     ★ 约束 1：`am startservice` 在**游戏处于后台时会失败**
       （Android 8+ 后台启动限制报 `Error: app is in background ...`）。
     ★ 约束 2（更致命）：游戏进程**不存在**时 `am startservice` 会新拉起一个进程，
       那里原生库尚未初始化，`Launcher.onCreate -> new Menu -> Menu.Icon()` 直接抛
       `UnsatisfiedLinkError: No implementation found for Java_com_android_support_Menu_Icon`，
       **把整个游戏进程搞崩**。所以调起前必须先确认进程在跑，否则直接放弃、走 prefs 降级。
  3. 悬浮窗存活秒数 = prefs 键 `-98`（mod 的 ShowMenu 里 postDelayed(Menu$1, -98*1000)），
     用户可在 mod 面板里设置。实测**每次触摸都会重新计时**，超时后 Menu$1 removeView
     真把窗口杀掉（不是隐藏）。⇒ 操作期间一直在交互就不会被中途杀掉；做完要等它自灭。
  4. 面板「常用」页的实际布局（1280x720 屏，面板矩形 (456,6)-(891,507)，即 435x501）：
        攻击倍率:  1000   [——●]         <- prefs 键 1
        防御倍率:  1000   [——●]         <- prefs 键 2
        舰船装填倍率: 1000 [——●]         <- prefs 键 3
        以德服人           [开关]        <- prefs 键 35
     「倍率」是 3 个 **SeekBar 滑块**，「以德服人」是唯一的**开关**。

★★ 为什么用滑块、绝不用「点数字弹输入框」★★
  点倍率数字确实会弹出 AlertDialog（标题 `Input number`、空 EditText、CANCEL/OK），
  看起来能精确输入 1 —— **但那个对话框的 OK 按钮是坏的**，实测按下即崩游戏：
      java.lang.ClassCastException: com.android.support.Launcher cannot be cast to
      android.app.Activity
        at Menu.ME017 → Menu.lambda$SeekBar$5 → AlertController$ButtonHandler.onClick
  根因：Menu 的 Context 是 `Launcher`（一个 Service），而 ME017 里做了 `(Activity) ctx`。
  ⇒ 本后端绝不触碰数字，也不打开任何对话框。

滑块的正确手势（实测）：
  - 点轨道**右端** → 精确 **1000**（单次点击，最快）。
  - 从右端**拖过左边界** → 精确 **1**。
      * 注意：只是「点」轨道左端得到的是 4 而不是 1（SeekBar 的 thumb 有内缩），
        必须**按住拖出左边界**才会 clamp 到最小值。
      * 拖拽起点必须落在轨道或 thumb 上；起点放到轨道外面会抓不住（实测无效）。

独立校验（两道）：
  1. **权威**：读共享配置 XML。mod 每次改值都会立刻把新值写回 XML，
     所以 XML 等价于它的内存状态，可精确比对。
  2. **截图**（OverlayVisualVerify，默认开）：从截图上找滑块 thumb 的青色圆点位置反解数值，
     再采样「以德服人」开关的颜色判断开/关。这把「三个倍率是不是都真的变成 1」
     用人眼可复现的方式再确认一遍。识图读不出来只记日志，不据此判失败。

失败兜底：调不起悬浮窗、坐标对不上、或核对不通过时，按 `OverlayFallbackPrefs`
（默认关，打开后生效）降级到 ModPrefs（写 XML + 重启游戏）。宁可多一次重启，
也不能让敏感任务带着倍率跑 —— 那正是这个功能要防的封号风险。
"""
import re
import time

import numpy as np

from module.config.deep import deep_get
from module.logger import logger

try:
    from module.base.base import ModuleBase
except ImportError:  # pragma: no cover - webui 进程 PIL 被换成假模块时的降级
    class ModuleBase:
        def __init__(self, config=None, device=None, task=None):
            self.config = config
            self.device = device

# 面板矩形内的相对坐标（比例；来自 1280x720 实测，(456,6)-(891,507) = 435x501）。
# 用比例而不是像素，是为了在密度/分辨率变化时仍然对得上（面板是固定 dp 布局）。
REL_SLIDER_Y = (0.321, 0.506, 0.687)   # 三个倍率滑块轨道的 y（0/1/2 行）
# 键 -> 面板滑块行号的显式映射（REL_SLIDER_Y 的下标）。
# 必须显式映射：配置里只填部分键时，不能拿「已配置键的排序子集」当行号，
# 否则 OnKeys='1=1000,3=1000' 会把键 3（装填）当成第 1 行去拖——那是倍防的轨道。
# {'1':0,'2':1,'3':2} 来自默认配置 OffKeys='1=1,2=1,3=1' 的实测（cfc364b3b）；
# 面板新增滑块时请先在真机上核对行序再补表。
SLIDER_KEY_ROW = {'1': 0, '2': 1, '3': 2}
REL_TRACK_X = (0.083, 0.929)           # 轨道左右端（点击/拖拽落点用）
REL_THUMB_X = (0.080, 0.917)           # thumb 在 最小值/最大值 时的中心 x（截图反解用）
REL_TOGGLE = (0.947, 0.771)            # 以德服人 开关中心
REL_TOGGLE_BOX = (0.86, 0.73, 1.00, 0.82)   # 开关采样框 (x0,y0,x1,y1) 比例
EXPANDED_MIN_RATIO = 0.15   # 面板宽度 > 屏宽 15% 即认为已展开（收起小球仅约 2%）

# 截图反解 thumb 时的颜色判据（thumb 是不透明的 Material 青 #80CBC4，
# 面板半透明但 thumb 实心，所以这一条不会被游戏背景污染）。
_THUMB_TOL = 70
_THUMB_MIN_PIXELS = 8


def _safe_int(text, default=None):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


class ModOverlay(ModuleBase):
    # 一旦检测到「调起悬浮窗把游戏搞崩（pid 变化/消失）」，本进程内永久停用 overlay，
    # 后续全部走 prefs 降级 —— 绝不在崩溃上重试，避免把游戏打进崩溃循环。
    # 类属性：alas.py 的调度循环每个任务边界都会 new 一个 ModHandler（后端随之
    # 重建），实例属性只活一轮；类属性才能跨任务边界保持「已停用」状态。
    _disabled = False

    def __init__(self, config=None, device=None):
        super().__init__(config=config, device=device)
        self.config = config
        self.device = device
        self._prefs = None
        self._screen = None

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

    def _write_survival(self):
        """
        把 prefs 的 -98（悬浮窗存活秒数）抬到配置值，让面板有更充裕的操作时间。

        ★ 只能在**游戏没运行时**写：游戏运行中改文件会被进程退出时的回写覆盖掉，
        而且我们不想为了这一项就把游戏停掉（那就成了"多余的重启"）。
        所以这是"尽力而为"：等哪天游戏恰好是关着的（比如 ALAS 停在这台机器上），
        下次启动就生效。
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

        空 target 一律判不命中（与 ModPrefs._match / _match_prefs 保持一致）：
        否则「没有任何要检查的键」会被当成「已经达成」，调用方会误判成功。
        """
        if not target:
            return False
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

    def _bounds(self, key):
        """
        该键在 OffKeys / OnKeys 里的取值区间 (lo, hi)。
        用途：判断该往滑块哪一端拖；截图反解数值时也要用它做线性换算。
        """
        vals = []
        for mode in (False, True):
            v = self._target(mode).get(str(key))
            if isinstance(v, bool) or v is None:
                continue
            vals.append(int(v))
        if not vals:
            return 1, 1000
        return min(vals), max(vals)

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

    @staticmethod
    def _parse_window(out, gravity):
        """
        从 dumpsys window windows 里找游戏进程 APPLICATION_OVERLAY 窗口的矩形并返回 (x0,y0,w,h)。

        gravity: 'TOP'   → 悬浮窗面板（gr=TOP|LEFT|CENTER，收起时是 24x39 的小球）
                 'CENTER'→ mod 的 AlertDialog（我们有意识地不使用它，仅用于排查）
        """
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
            if 'APPLICATION_OVERLAY' not in text:
                continue
            if not re.search(r'gr=\s*' + gravity, text):
                continue
            m = re.search(r'mFrame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', line)
            if not m:
                continue
            x0, y0, x1, y1 = (int(v) for v in m.groups())
            if x1 > x0 and y1 > y0:
                return x0, y0, x1 - x0, y1 - y0
        return None

    def overlay_frame(self):
        """
        取悬浮窗矩形 (x0, y0, w, h)，取不到返回 None。

        判据：同一窗口块里同时出现 APPLICATION_OVERLAY 与 gr=TOP*（悬浮窗是 TOP|LEFT|CENTER
        对齐；mod 的 AlertDialog 是 gr=CENTER，靠这个区分）。
        APPLICATION_OVERLAY 只由带 overlay 权限的窗口使用，不会误伤普通 Activity。
        """
        try:
            out = str(self.device.adb_shell(['dumpsys', 'window', 'windows'], timeout=40) or '')
        except Exception as e:
            logger.warning(f'ModOverlay: dumpsys window failed: {e}')
            return None
        return self._parse_window(out, 'TOP')

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
        """
        点击。优先用 ALAS 自己的 control 通道（Emulator_ControlMethod，本机是 MaaTouch，
        常驻 socket，实测 50ms 级；比 `adb shell input` 每次都起进程快且稳），
        失败再退回 adb。
        """
        x, y = int(x), int(y)
        try:
            from module.base.button import Button
            btn = Button(area=(x - 3, y - 3, x + 4, y + 4), color=(),
                         button=(x - 3, y - 3, x + 4, y + 4), name=f'ModOverlay({x},{y})')
            # control_check=False：绕开 Device 的连点计数保护（这里是我们自己的坐标，
            # 不是游戏内按钮），否则可能误触 GameTooManyClickError。
            self.device.click(btn, control_check=False)
            return True
        except Exception as e:
            logger.info(f'ModOverlay: device.click 不可用（{type(e).__name__}: {e}），退回 adb input tap')
        try:
            self.device.adb_shell(['input', 'tap', str(x), str(y)])
            return True
        except Exception as e:
            logger.warning(f'ModOverlay: tap ({x},{y}) failed: {e}')
            return False

    def _drag(self, p1, p2):
        """按住拖动。同样优先走 ALAS 的 control 通道。"""
        p1 = (int(p1[0]), int(p1[1]))
        p2 = (int(p2[0]), int(p2[1]))
        try:
            self.device.swipe(p1, p2, duration=(0.25, 0.35), name='ModOverlay.Slider')
            return True
        except Exception as e:
            logger.info(f'ModOverlay: device.swipe 不可用（{type(e).__name__}: {e}），退回 adb input swipe')
        try:
            self.device.adb_shell(['input', 'swipe', str(p1[0]), str(p1[1]),
                                   str(p2[0]), str(p2[1]), '300'])
            return True
        except Exception as e:
            logger.warning(f'ModOverlay: swipe {p1}->{p2} failed: {e}')
            return False

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
            time.sleep(0.3)
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
            time.sleep(0.15)
            cur = self.overlay_frame()
            if cur and self._expanded(cur):
                return cur
        logger.warning('ModOverlay: 悬浮窗没能展开')
        return None

    @staticmethod
    def _row_point(frame, rel):
        x0, y0, w, h = frame
        return int(x0 + rel[0] * w), int(y0 + rel[1] * h)

    def _gesture_slider(self, frame, row, to_max):
        """
        把第 row 个滑块推到最大/最小。

        实测（1280x720）：
          点轨道右端 → 精确 1000
          从右端拖过左边界 → 精确 1（注意只「点」左端会得到 4，必须拖出边界才会 clamp）
        """
        x0, y0, w, h = frame
        y = int(y0 + REL_SLIDER_Y[row] * h)
        xl = int(x0 + REL_TRACK_X[0] * w)
        xr = int(x0 + REL_TRACK_X[1] * w)
        if to_max:
            return self._tap(xr, y)
        return self._drag((xr, y), (xl - 40, y))

    def _gesture_toggle(self, frame):
        """点「以德服人」开关（整行右侧的 Switch，点一下翻转）。"""
        x, y = self._row_point(frame, REL_TOGGLE)
        return self._tap(x, y)

    # ------------------------------------------------------------ 截图复核
    @staticmethod
    def _thumb_center(image, y, x_from, x_to, half=9):
        """
        在 y 附近、x ∈ [x_from, x_to] 的横带里找滑块 thumb（实心青色圆）的中心 x。
        返回 None 表示没找到。

        ★ 必须限定在面板横向范围内扫：面板是半透明的，扫整屏会吃到游戏背景里的
        青色 UI（实测会把 thumb 认到面板外面去，读出的值完全错）。
        """
        h, w, _ = image.shape
        y0 = max(0, y - half)
        y1 = min(h, y + half + 1)
        xa = max(0, int(x_from))
        xb = min(w, int(x_to))
        if y1 <= y0 or xb <= xa:
            return None
        sub = image[y0:y1, xa:xb].astype(np.int16)
        # Material Teal 200 (#80CBC4): R≈128 G≈203 B≈196
        mask = ((np.abs(sub[:, :, 0] - 128) < _THUMB_TOL)
                & (np.abs(sub[:, :, 1] - 203) < _THUMB_TOL)
                & (np.abs(sub[:, :, 2] - 196) < _THUMB_TOL))
        counts = mask.sum(axis=0)
        idx = np.where(counts >= _THUMB_MIN_PIXELS)[0]
        if idx.size == 0:
            return None
        return int(xa + (idx[0] + idx[-1]) / 2)

    def _read_slider_values(self, image, frame, int_keys):
        """从截图反解滑块的数值；读不出来的键、不在行映射表里的键都不放进结果。"""
        x0, y0, w, h = frame
        a = x0 + REL_THUMB_X[0] * w
        b = x0 + REL_THUMB_X[1] * w
        out = {}
        for key in int_keys:
            row = SLIDER_KEY_ROW.get(str(key))
            if row is None or row >= len(REL_SLIDER_Y):
                continue
            cx = self._thumb_center(image, int(y0 + REL_SLIDER_Y[row] * h),
                                    x0, x0 + w)
            if cx is None:
                continue
            lo, hi = self._bounds(key)
            frac = 0.0 if b <= a else (cx - a) / (b - a)
            frac = max(0.0, min(1.0, frac))
            out[str(key)] = int(round(lo + frac * (hi - lo)))
        return out

    @staticmethod
    def _read_toggle(image, frame):
        """
        从截图判断「以德服人」开关的开/关。

        判据：看采样框**右半边**的绿度（G-R 均值）。
          - 开：亮绿滑块停在右侧 ⇒ 右半边 G-R 很高（实测 ≈ 119）
          - 关：滑块停在左侧、右边只剩暗底 ⇒ 右半边 G-R 很低（实测 ≈ -1）
        读数落在中间地带（背景干扰等）时返回 None，交给 prefs 判定，不据此判失败。
        """
        x0, y0, w, h = frame
        bx0 = int(x0 + REL_TOGGLE_BOX[0] * w)
        by0 = int(y0 + REL_TOGGLE_BOX[1] * h)
        bx1 = int(x0 + REL_TOGGLE_BOX[2] * w)
        by1 = int(y0 + REL_TOGGLE_BOX[3] * h)
        box = image[max(0, by0):by1, max(0, bx0):bx1].astype(np.int16)
        if box.size == 0:
            return None
        half = box.shape[1] // 2
        left, right = box[:, :half], box[:, half:]
        if left.size == 0 or right.size == 0:
            return None
        lg = float(right[:, :, 1].mean() - right[:, :, 0].mean())
        ll = float(left[:, :, 1].mean() - left[:, :, 0].mean())
        logger.attr('ModOverlay.toggle(visual)', f'left G-R={ll:.0f}  right G-R={lg:.0f}')
        if lg >= 60:
            return True
        if lg <= 20:
            return False
        return None

    def _verify_visual(self, frame, target):
        """
        截图复核（尽力而为，第二道确认）：
          - 三个倍率：在截图上找滑块 thumb 的位置反解数值，和目标比对；
          - 以德服人：采样开关区域颜色判断开/关。
        权威判据始终是 prefs XML；识图读不出来只记日志，不判失败。
        """
        try:
            image = self.device.screenshot()
        except Exception as e:
            logger.warning(f'ModOverlay: screenshot failed: {e}')
            return None
        int_keys = sorted(k for k, v in target.items() if not isinstance(v, bool))
        bool_keys = [k for k, v in target.items() if isinstance(v, bool)]
        result = {}
        try:
            got = self._read_slider_values(image, frame, int_keys)
            want = {str(k): int(v) for k, v in target.items() if not isinstance(v, bool)}
            # thumb 中心是亚像素级检测，±1px ≈ 2.7 个数值单位（1..1000 的轨道约 364px），
            # 所以给与量级匹配的容差；仍能抓住"该是 1 却还是 1000"这种量级错误。
            mismatched = []
            unreadable = []
            for k, v in want.items():
                g = got.get(k)
                if g is None:
                    unreadable.append(str(k))
                    continue
                lo, hi = self._bounds(k)
                if abs(g - v) > max(3, (hi - lo) // 100):
                    mismatched.append(f'{k}:{g}≠{v}')
            result['numbers'] = got
            result['numbers_ok'] = not mismatched
            logger.attr('ModOverlay.verify(visual)',
                        f"numbers {got} target {want} -> "
                        f"{'OK' if not mismatched else 'MISMATCH ' + ' '.join(mismatched)}"
                        + (f' (unreadable {",".join(unreadable)})' if unreadable else ''))
        except Exception as e:
            logger.info(f'ModOverlay: 倍率识图跳过（{type(e).__name__}: {e}）')
        if bool_keys:
            try:
                got = self._read_toggle(image, frame)
                want = bool(target[bool_keys[0]])
                result['toggle'] = got
                result['toggle_ok'] = (got is None) or (got == want)
                logger.attr('ModOverlay.verify(visual)',
                            f"toggle {got} target {want} -> "
                            f"{'OK' if got == want else ('UNREADABLE' if got is None else 'MISMATCH')}")
            except Exception as e:
                logger.info(f'ModOverlay: 开关识图跳过（{type(e).__name__}: {e}）')
        return result or None

    # ------------------------------------------------------------ 清场
    def _stop_requested(self):
        """
        GUI 触发「更新 / 重启」时会 set 一个 Event，process_manager 把它注入到
        AzurLaneConfig.stop_event（类属性，调度进程内全局可见）。
        这时没必要再等悬浮窗自灭，尽快把控制权还给调度循环让它退出。
        直接跑 `python alas.py`（不经过 GUI）时该值为 None，视为没有停止请求。
        """
        event = getattr(self.config, 'stop_event', None)
        return event is not None and event.is_set()

    def wait_gone(self, extra=8, cap=180):
        """
        等悬浮窗被 mod 自己杀死（removeView）后再返回，避免面板留在屏幕上干扰
        Alas 后续的识图。

        ★ 不用「隐藏图标」也不用「关闭窗口」按钮来主动收尾：那两种都会改变 mod 的
        窗口状态（隐藏图标还可能被 Alas 或别的任务误触/重新弹出），无法预期。
        面板的存活计时是「每次触摸后重新计 -98 秒」，所以这里按设备真实 -98 轮询，
        一消失就立刻返回。

        轮询间隔取 1s：单次 `dumpsys window windows` 本身就要几百 ms，
        更密的轮询只会徒增 adb 负担。等待中途收到 GUI 的停止请求（更新/重启）
        时立即让出，不再把调度循环拖住最坏 survival+extra 秒。
        """
        deadline = time.time() + min(cap, self.survival_seconds + extra)
        while time.time() < deadline:
            if self._stop_requested():
                logger.info('ModOverlay: wait_gone interrupted by stop event')
                return False
            if not self.overlay_frame():
                logger.attr('ModOverlay', 'overlay gone, screen clean')
                return True
            time.sleep(1.0)
        if self.overlay_frame():
            logger.warning('ModOverlay: 悬浮窗仍未消失（超过 %ss），后续识图可能受干扰'
                           % int(min(cap, self.survival_seconds + extra)))
            return False
        return True

    # ------------------------------------------------------------ 主入口
    def set_multiplier(self, mode: bool):
        """
        把倍率切到目标状态，全程不重启游戏。

        Args:
            mode: True = 开倍率（OnKeys），False = 关倍率（OffKeys）
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
                    # 走 prefs_reader 复用实例：既省一次构造，也让降级链路可被
                    # 自检脚本用桩替换（dev_tools/mod_overlay_selftest.py）
                    changed = self.prefs_reader.set_multiplier(mode)
                    return bool(changed)
                except Exception as e:
                    logger.error(f'ModOverlay: prefs 降级也失败: {type(e).__name__}: {e}')
            else:
                logger.critical('ModOverlay: 未生效且已关闭降级，'
                                '敏感任务可能带着倍率开打，请立刻检查！')
            return False
        return True

    def _pending(self, target, int_keys, bool_keys):
        """按设备真实状态算出还差哪些键没达成。"""
        cur = self._prefs_raw() or {}
        todo_int = [k for k in int_keys
                    if not self._at_target(cur, {k: target[k]},
                                           tol_int=self._tolerance({k: target[k]}))]
        todo_bool = [k for k in bool_keys
                     if not self._at_target(cur, {k: target[k]})]
        return todo_int, todo_bool

    def _apply(self, target, int_keys, bool_keys, mode):
        """
        一次调起悬浮窗，把**所有**还没达成的键都改掉。

        为什么现在敢一次做完：实测一次「调起 + 展开 + 3 次拖滑块 + 1 次点开关」约 4~5 秒，
        而面板存活期 -98 = 20 秒，而且**每次触摸都会重新计时**，所以中途不会被杀掉。
        （旧实现一次只改一个键，是因为当时误用了 uiautomator2，冷启动就能吃掉十几秒。）
        """
        limit = 4
        for attempt in range(1, limit + 1):
            todo_int, todo_bool = self._pending(target, int_keys, bool_keys)
            if not todo_int and not todo_bool:
                return True

            pid_before = self._game_pid()
            if not self.show():
                return False
            frame = self.expand()
            if not frame:
                self.wait_gone()
                continue

            logger.hr('ModOverlay', level=1)
            logger.info(f'ModOverlay: 目标 {target} | 待改 int={todo_int} bool={todo_bool}')

            for key in todo_int:
                # 行号来自显式映射表，不是「已配置键的排序位置」——
                # 配置只填部分键时后者会拖错别人的轨道
                row = SLIDER_KEY_ROW.get(str(key))
                if row is None or row >= len(REL_SLIDER_Y):
                    logger.warning(
                        f'ModOverlay: 键 {key} 不在滑块行映射表 {SLIDER_KEY_ROW} 内，'
                        f'跳过（面板行序有变时请在真机核对后更新 SLIDER_KEY_ROW）')
                    continue
                lo, hi = self._bounds(key)
                want = int(target[key])
                to_max = want >= hi
                if lo < hi and want not in (lo, hi):
                    logger.warning(
                        f'ModOverlay: 键 {key} 目标值 {want} 不是极值（{lo}/{hi}），'
                        f'滑块手势只能拖到极值，将拖到{"最大" if to_max else "最小"}，'
                        f'后续校验可能失败')
                logger.info(f'ModOverlay: 键 {key} -> {want}（滑块拖到{"最大" if to_max else "最小"}）')
                self._gesture_slider(frame, row, to_max)
                time.sleep(0.5)
                if not self._alive(frame):
                    logger.info('ModOverlay: 面板中途消失，重新调起后继续')
                    break
            else:
                for key in todo_bool:
                    logger.info(f'ModOverlay: 键 {key} -> {target[key]}（点开关）')
                    self._gesture_toggle(frame)
                    time.sleep(0.6)
                    if not self._alive(frame):
                        logger.info('ModOverlay: 面板中途消失，重新调起后继续')
                        break
                else:
                    cur = self._prefs_raw() or {}
                    if self.visual_verify:
                        try:
                            self._verify_visual(frame, target)
                        except Exception as e:
                            logger.info(f'ModOverlay: 截图复核跳过（{type(e).__name__}: {e}）')
                    if self._at_target(cur, target, tol_int=self._tolerance(target)):
                        pid_after = self._game_pid()
                        if not pid_after or pid_after != pid_before:
                            self._crash_guard(pid_before, pid_after)
                            return False
                        self.wait_gone()
                        return True

            # 崩溃检测：正常操作前后游戏 pid 必须一致。变了/没了说明我们把游戏搞崩了。
            pid_after = self._game_pid()
            if not pid_after or pid_after != pid_before:
                self._crash_guard(pid_before, pid_after)
                return False

            self.wait_gone()
            logger.warning(f'ModOverlay: 第 {attempt} 次未收敛，重试（int={todo_int} bool={todo_bool}）')
        return False

    def _alive(self, frame):
        cur = self.overlay_frame()
        return bool(cur) and self._expanded(cur)

    def _crash_guard(self, pid_before, pid_after):
        # 写类属性：实例上赋值只会遮蔽本实例，新的 ModOverlay 实例又会看到 False
        ModOverlay._disabled = True
        logger.critical(
            f'ModOverlay: 调起悬浮窗后游戏进程消失/重启（pid {pid_before!r} -> {pid_after!r}）。'
            f'本进程内永久停用 overlay 后端，改用 prefs 降级。'
            f'根因通常是 Mod 的 x86_64 原生库没有实现 Menu.Icon()：'
            f'进程不在时 startservice 会新起进程，Launcher.onCreate -> new Menu -> Menu.Icon() '
            f'抛 UnsatisfiedLinkError，整个游戏进程崩溃。')

    def repair(self, mode: bool):
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
            'visual_verify': self.visual_verify,
            'disabled': self._disabled,
            'game_pid': None,
            'game_foreground': None,
            'overlay_frame': None,
            'prefs_state': None,
        }
        try:
            info['game_pid'] = self._game_pid()
        except Exception as e:
            info['game_pid'] = f'error: {e}'
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
