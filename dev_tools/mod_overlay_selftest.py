"""
ModOverlay 自检 —— 默认后端（悬浮窗直点）的纯逻辑验证。

为什么单独有这个套件：Backend 默认值是 overlay，但此前它没有任何自测覆盖
（策略/prefs/ui/e2e 四个套件都不 import 它），而恰恰是它出过三个真 bug：
  * 滑块行号按「已配置键的排序子集」算 -> 配置只填部分键时拖错别人的轨道
  * 崩溃保护 _disabled 是实例属性 -> alas.py 每个任务边界都 new 一次后端，保护每轮丢失
  * 目标值不是极值时手势拖不到，静默失败

不需要真机：窗口矩形从合成 dumpsys 文本解析，滑块/开关从合成 numpy 截图上反解，
prefs 走 testkit 的 FakeDevice 假 adb。真机相关的验证仍用 dev_tools/mod_handler_doctor.py。
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mod_handler_testkit import (  # noqa: E402
    Checker, FakeConfig, FakeDevice, fake_logger, install_stubs,
)

install_stubs()

from module.mod_handler import mod_overlay as mo  # noqa: E402
from module.mod_handler.mod_overlay import (  # noqa: E402
    ModOverlay, REL_SLIDER_Y, REL_THUMB_X, REL_TRACK_X, REL_TOGGLE_BOX, SLIDER_KEY_ROW,
)

checker = Checker('ModOverlay 自检')
check = checker.check
eq = checker.eq

# 实测的面板矩形（1280x720 屏，面板 (456,6)-(891,507) = 435x501）
FRAME = (456, 6, 435, 501)
COLLAPSED = (1200, 6, 24, 39)
SCREEN = (1280, 720)
TEAL = (128, 203, 196)          # #80CBC4，滑块 thumb 的实测颜色


class OverlayDevice(FakeDevice):
    """在 FakeDevice 上补上 overlay 用到的 dumpsys / wm size 通道。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.windows_dump = ''
        self.focus_dump = ''
        self.overlay_alive = True
        # 模拟 shell 启动未导出 Service 被拒（现场：Requires permission not
        # exported from uid 10046）。设为错误串后，普通 startservice 返回它，
        # su -c 的 startservice 模拟 root 成功（返回空）。
        self.startservice_error = None
        # False 时模拟 su 通道不可用（su 命令返回 Error），用于测无 root 兜底
        self.su_fails = False

    def adb_shell(self, cmd, timeout=10, **kwargs):
        if isinstance(cmd, (list, tuple)):
            cmd = ' '.join(str(c) for c in cmd)
        cmd = str(cmd)
        if cmd.startswith('wm size'):
            self.calls.append(cmd)
            return f'Physical size: {SCREEN[0]}x{SCREEN[1]}'
        if cmd.startswith('dumpsys window windows'):
            self.calls.append(cmd)
            return self.windows_dump
        if cmd.startswith('dumpsys window'):
            self.calls.append(cmd)
            return self.focus_dump
        if cmd.startswith('su -c') and 'am stopservice' in cmd:
            self.calls.append(cmd)
            return 'Error: su denied' if self.su_fails else ''
        if cmd.startswith('su -c') and 'am startservice' in cmd:
            self.calls.append(cmd)
            if self.su_fails:
                return 'Error: su denied'
            # 模拟 root 重建 Service 成功：mod 把小球重新放回屏幕
            self.windows_dump = self.windows_dump or DUMP
            return ''
        if 'am startservice' in cmd and self.startservice_error:
            self.calls.append(cmd)
            return self.startservice_error
        return super().adb_shell(cmd, timeout=timeout)


def make_overlay(**values):
    """建一个 ModOverlay + 假设备；默认配置与 testkit 的 FakeConfig 一致。"""
    cfg = FakeConfig(**values)
    dev = OverlayDevice()
    return ModOverlay(config=cfg, device=dev), cfg, dev


def set_device_xml(overlay, xml):
    """
    改「设备上」的 prefs 内容，并让 overlay 的快照缓存失效。

    真实场景里 XML 是 mod 自己（或用户手动）改的，overlay 无从得知；测试里
    改了设备内容就必须显式失效，否则会读到改之前的快照。overlay 自身在
    点击/写盘之后都会自动失效，这里模拟的是「设备在背后变了」。
    """
    overlay.device.xml = xml
    overlay._invalidate_prefs()


def image_with_dot(x, y, size=14, color=TEAL):
    """黑底图 + 一个方形色块（模拟滑块 thumb）。"""
    img = np.zeros((SCREEN[1], SCREEN[0], 3), dtype=np.uint8)
    img[:, :] = (30, 30, 30)
    img[max(0, y - size // 2):y + size // 2, max(0, x - size // 2):x + size // 2] = color
    return img


def xml_of(**keys):
    """构造 prefs XML 文本（int / bool 都支持）。"""
    body = []
    for name, value in keys.items():
        tag = 'boolean' if isinstance(value, bool) else 'int'
        body.append(f'    <{tag} name="{name}" value="'
                    f'{"true" if value is True else "false" if value is False else value}" />')
    return ("<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n<map>\n"
            + '\n'.join(body) + '\n</map>\n')


def window_block(title, attrs, frame):
    return (f'  Window #{title}:\n'
            f'    mOwnerUid=10046\n'
            f'    mAttrs={{{attrs}}}\n'
            f'    mFrame=[{frame}] last=[{frame}]\n')


# ---------------------------------------------------------------- 1. 行映射表
checker.header('1. 滑块行映射表（bug ③ 的回归）')
check('SLIDER_KEY_ROW 覆盖默认三个倍率键', set(SLIDER_KEY_ROW) >= {'1', '2', '3'},
      str(SLIDER_KEY_ROW))
eq('键 1 -> 行 0（攻击倍率）', SLIDER_KEY_ROW.get('1'), 0)
eq('键 2 -> 行 1（防御倍率）', SLIDER_KEY_ROW.get('2'), 1)
eq('键 3 -> 行 2（舰船装填倍率）', SLIDER_KEY_ROW.get('3'), 2)
check('表里的行号都在 REL_SLIDER_Y 范围内',
      all(0 <= r < len(REL_SLIDER_Y) for r in SLIDER_KEY_ROW.values()))

# 关键防回归：只配 1 和 3 时，键 3 必须仍是行 2，不能变成「子集里的第 2 个」= 行 1
int_keys = ['1', '3']
rows = [SLIDER_KEY_ROW.get(k) for k in int_keys]
eq('只配 1/3 时行号仍是 [0, 2]（旧实现会算成 [0, 1]）', rows, [0, 2])

# ---------------------------------------------------------------- 2. 目标值语义
checker.header('2. _bounds / _tolerance / _at_target')
o, cfg, dev = make_overlay()
eq('_bounds("1") 取两端 = (1, 1000)', o._bounds('1'), (1, 1000))
eq('_bounds("35") 布尔键回退到 (1, 1000)', o._bounds('35'), (1, 1000))
eq('_tolerance(OffKeys=1) = 0（1 必须精确）', o._tolerance({'1': 1, '2': 1, '3': 1}), 0)
eq('_tolerance(OnKeys=1000) = 10（1% 量级容差）',
   o._tolerance({'1': 1000, '2': 1000, '3': 1000}), 10)

parsed_off = {'1': ('int', '1'), '2': ('int', '1'), '3': ('int', '1'), '35': ('boolean', 'false')}
parsed_on = {'1': ('int', '1000'), '2': ('int', '1000'), '3': ('int', '1000'), '35': ('boolean', 'true')}
parsed_mid = {'1': ('int', '500'), '2': ('int', '500'), '3': ('int', '500'), '35': ('boolean', 'true')}

check('_at_target: 全 1 命中 OffKeys', o._at_target(parsed_off, {'1': 1, '2': 1, '3': 1}))
check('_at_target: 布尔 false 命中', o._at_target(parsed_off, {'35': False}))
check('_at_target: 1005 在 1000 的容差内',
      o._at_target({'1': ('int', '1005')}, {'1': 1000}, tol_int=10))
check('_at_target: 1011 超出容差',
      not o._at_target({'1': ('int', '1011')}, {'1': 1000}, tol_int=10))
check('_at_target: 键缺失即不命中', not o._at_target({}, {'1': 1}))
check('_at_target: 空 target 判不命中（否则会被当成「已达成」）',
      not o._at_target(parsed_on, {}))

# ---------------------------------------------------------------- 3. get_state 三态
checker.header('3. get_state 三态（读 prefs）')


def state_with(xml):
    o2, _, _ = make_overlay()
    o2._prefs = None
    set_device_xml(o2, xml)
    return o2.get_state()


eq('关状态 XML -> False', state_with(xml_of(**{'1': 1, '2': 1, '3': 1})), False)
eq('开状态 XML -> True', state_with(xml_of(**{'1': 1000, '2': 1000, '3': 1000})), True)
eq('中间值 XML -> None', state_with(xml_of(**{'1': 500, '2': 500, '3': 500})), None)
eq('读不到 XML -> None', state_with(''), None)

# 部分键的配置：只有 1 和 3，必须两个都对上才算开
o3, _, _ = make_overlay(OffKeys='1=1,3=1', OnKeys='1=1000,3=1000')
set_device_xml(o3, xml_of(**{'1': 1000, '2': 1, '3': 1000}))
eq('部分键配置下两个键都对上 -> True', o3.get_state(), True)
set_device_xml(o3, xml_of(**{'1': 1000, '2': 1, '3': 1}))
eq('部分键配置下只对上一个是 -> None', o3.get_state(), None)

# ---------------------------------------------------------------- 4. 窗口矩形解析
checker.header('4. _parse_window（dumpsys window windows）')
DUMP = (
    window_block('0 Window{a1 u0 com.bilibili.azurlane/com.bilibili.azurlane.MainActivity}',
                 '(0,0)(1280,720) gr=TOP|LEFT|CENTER sim={} ty=APPLICATION fl=FULLSCREEN',
                 '0,0][1280,720')
    + window_block('1 Window{a2 u0 com.bilibili.azurlane/com.android.support.Launcher}',
                   '(456,6)(435,501) gr=TOP|LEFT|CENTER sim={} ty=APPLICATION_OVERLAY fl=NOT_FOCUSABLE',
                   '456,6][891,507')
    + window_block('2 Window{a3 u0 com.bilibili.azurlane}',
                   '(400,200)(300,200) gr=CENTER sim={} ty=APPLICATION_OVERLAY fl=NOT_FOCUSABLE',
                   '400,200][700,400')
)
eq('取 TOP 悬浮窗（跳过 Activity 与 CENTER 对话框）',
   ModOverlay._parse_window(DUMP, 'TOP', package='com.bilibili.azurlane'), (456, 6, 435, 501))
eq('取 CENTER 对话框（排查用）',
   ModOverlay._parse_window(DUMP, 'CENTER', package='com.bilibili.azurlane'), (400, 200, 300, 200))

DUMP_COLLAPSED = window_block(
    '1 Window{a2 u0 com.bilibili.azurlane/com.android.support.Launcher}',
    '(1200,6)(24,39) gr=TOP|LEFT|CENTER sim={} ty=APPLICATION_OVERLAY fl=NOT_FOCUSABLE',
    '1200,6][1224,45')
eq('收起态小球矩形', ModOverlay._parse_window(DUMP_COLLAPSED, 'TOP', package='com.bilibili.azurlane'), COLLAPSED)
eq('空输出 -> None', ModOverlay._parse_window('', 'TOP', package='com.bilibili.azurlane'), None)
OTHER_APP = window_block('9 Window{zz u0 com.example.other/SomeOverlay}',
                         '(456,6)(435,501) gr=TOP|LEFT|CENTER sim={} ty=APPLICATION_OVERLAY fl=NOT_FOCUSABLE',
                         '456,6][891,507')
eq('别的 App 的 TOP 悬浮窗 -> 不过滤会误判，加包名后为 None',
   ModOverlay._parse_window(OTHER_APP, 'TOP', package='com.bilibili.azurlane'), None)
eq('没有 overlay 窗口 -> None',
   ModOverlay._parse_window(window_block('0 Window{a1}', '(0,0)(1280,720) ty=APPLICATION',
                                        '0,0][1280,720'), 'TOP', package='com.bilibili.azurlane'), None)

o4, _, dev4 = make_overlay()
dev4.windows_dump = DUMP
eq('overlay_frame() 走设备通道', o4.overlay_frame(), (456, 6, 435, 501))
eq('_screen_size() 解析 wm size', o4._screen_size(), SCREEN)
check('_expanded: 面板 435px 宽 -> True', o4._expanded(FRAME) is True)
check('_expanded: 收起小球 24px -> False', o4._expanded(COLLAPSED) is False)
check('_expanded: 无 frame -> False', o4._expanded(None) is False)

# ---------------------------------------------------------------- 5. 截图反解
checker.header('5. 截图反解滑块与开关（合成图）')
o5, _, _ = make_overlay()

# 每个键的滑块行 y 与 1000 / 1 对应的 thumb 中心 x
def row_y(row):
    return int(FRAME[1] + REL_SLIDER_Y[row] * FRAME[3])


x_at_max = int(FRAME[0] + REL_THUMB_X[1] * FRAME[2])
x_at_min = int(FRAME[0] + REL_THUMB_X[0] * FRAME[2])

img = image_with_dot(x_at_max, row_y(0))          # 只有键 1 的滑块在最大处
got = o5._read_slider_values(img, FRAME, ['1'])
check('键 1 的滑块反解为 ~1000', got.get('1') is not None and abs(got['1'] - 1000) <= 5, str(got))

# 关键防回归：键 3 的滑块画在第 2 行，反解必须落在键 3 上。
# 旧实现（sorted(int_keys).index）会把 ['1','3'] 里的键 3 当成行 1 -> 读不到。
img = image_with_dot(x_at_max, row_y(2))
got = o5._read_slider_values(img, FRAME, ['1', '3'])
check('键 3 从第 2 行反解出来（旧实现会去读第 1 行而读空）',
      got.get('3') is not None and abs(got['3'] - 1000) <= 5, str(got))
check('同一张图里键 1 没画滑块 -> 不放进结果', '1' not in got, str(got))

img = image_with_dot(x_at_min, row_y(2))
got = o5._read_slider_values(img, FRAME, ['3'])
check('键 3 在最左端反解为 ~1', got.get('3') is not None and abs(got['3'] - 1) <= 5, str(got))

img = image_with_dot(10, 10)                       # 面板外有个青色块
got = o5._read_slider_values(img, FRAME, ['1'])
eq('面板横向范围外扫不到 thumb（不误读游戏背景）', got, {})
got = o5._read_slider_values(image_with_dot(x_at_max, row_y(0)), FRAME, ['99'])
eq('不在行映射表里的键被跳过', got, {})


def toggle_image(right_rgb, left_rgb=(60, 60, 60)):
    img = np.zeros((SCREEN[1], SCREEN[0], 3), dtype=np.uint8)
    img[:, :] = (30, 30, 30)
    bx0 = int(FRAME[0] + REL_TOGGLE_BOX[0] * FRAME[2])
    by0 = int(FRAME[1] + REL_TOGGLE_BOX[1] * FRAME[3])
    bx1 = int(FRAME[0] + REL_TOGGLE_BOX[2] * FRAME[2])
    by1 = int(FRAME[1] + REL_TOGGLE_BOX[3] * FRAME[3])
    half = (bx1 - bx0) // 2
    img[by0:by1, bx0:bx0 + half] = left_rgb
    img[by0:by1, bx0 + half:bx1] = right_rgb
    return img


eq('开关右半边亮绿 -> True', ModOverlay._read_toggle(toggle_image((0, 200, 0)), FRAME), True)
eq('开关右半边暗底 -> False', ModOverlay._read_toggle(toggle_image((60, 60, 60)), FRAME), False)
eq('开关读数落在中间地带 -> None（交给 prefs 判定）',
   ModOverlay._read_toggle(toggle_image((100, 140, 100)), FRAME), None)
eq('采样框为空 -> None', ModOverlay._read_toggle(np.zeros((0, 0, 3), dtype=np.uint8), FRAME), None)

# ---------------------------------------------------------------- 6. 崩溃保护
checker.header('6. 崩溃保护跨实例保持（bug ② 的回归）')
ModOverlay._disabled = False
a, cfg_a, dev_a = make_overlay()
check('初始未停用', a._disabled is False)
a._crash_guard('1234', '5678')
check('_crash_guard 后本实例停用', a._disabled is True)
b, cfg_b, dev_b = make_overlay()
check('_disabled 是类属性：新建实例仍然停用（旧实现这里会回到 False）',
      b._disabled is True)
check('_disabled 定义在类上，不在实例字典里', '_disabled' not in a.__dict__)

class StubPrefs:
    """记录调用的假 ModPrefs，用来验证降级链路。"""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def set_multiplier(self, mode):
        self.calls.append(mode)
        return self.result


# 停用后不得再碰悬浮窗：直接自动降级走 prefs（2026-09-12 起不再受开关控制）
set_device_xml(b, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
b.config.data['ModHandler']['ModHandler']['OverlayFallbackPrefs'] = False
b._prefs = StubPrefs(result=True)
dev_b.calls.clear()
eq('停用 + 降级开关关闭 -> 仍自动降级走 prefs', b.set_multiplier(False), True)
touched = [c for c in dev_b.calls
           if 'startservice' in c or 'stopservice' in c or 'input tap' in c or 'input swipe' in c]
eq('停用后完全不碰悬浮窗', touched, [])
eq('降级把 mode 透传给 prefs', b._prefs.calls, [False])
ModOverlay._disabled = False

# ---------------------------------------------------------------- 7. set_multiplier 主路径
checker.header('7. set_multiplier 主路径与降级')

# fallback_enabled 的缺省值必须与四处配置源一致（config_generated.py / args.json /
# argument.yaml / template.json 都是 false）。以前写成 default=True，配置里缺这个键
# 时会静默变成「允许为了关倍率重启游戏」，与文档和用户偏好相反。
# FakeConfig 默认就不带 OverlayFallbackPrefs，所以下面第一个用例走的就是「缺键」。
check('OverlayFallbackPrefs 缺省 -> 按默认关处理（与配置源一致）',
      make_overlay()[0].fallback_enabled is False)
check('OverlayFallbackPrefs=True 时才允许降级',
      make_overlay(OverlayFallbackPrefs=True)[0].fallback_enabled is True)


o7, cfg7, dev7 = make_overlay()
set_device_xml(o7, xml_of(**{'1': 1, '2': 1, '3': 1}))   # 设备上已经是关
dev7.calls.clear()
eq('已是目标状态 -> True（不动作）', o7.set_multiplier(False), True)
eq('已是目标状态时不碰设备', [c for c in dev7.calls if 'startservice' in c], [])

o8, cfg8, dev8 = make_overlay(OverlayFallbackPrefs=True)   # 开关已废弃，传什么行为一致
set_device_xml(o8, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
o8._prefs = StubPrefs(result=True)
o8._disabled = True                                # 强制走降级分支
eq('停用 -> 走 prefs 并返回其结果', o8.set_multiplier(False), True)
eq('降级时把 mode 透传给 ModPrefs', o8._prefs.calls, [False])

# 开关关闭时行为一致：自动降级不再受 OverlayFallbackPrefs 控制
o8b, cfg8b, dev8b = make_overlay()                 # OverlayFallbackPrefs 缺省 = 关
set_device_xml(o8b, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
o8b._prefs = StubPrefs(result=True)
o8b._disabled = True
eq('停用 + 降级开关关闭 -> 同样自动降级', o8b.set_multiplier(False), True)
eq('自动降级时透传给 prefs', o8b._prefs.calls, [False])

# 真实降级链路（不替换 prefs_reader）：停用 overlay + 开启降级 -> 真写 prefs
o9, cfg9, dev9 = make_overlay(OverlayFallbackPrefs=True)
o9._disabled = True                                # 跳过悬浮窗尝试，直接验证降级
set_device_xml(o9, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
dev9.running = True
dev9.calls.clear()
eq('真实降级链路（overlay 失败 -> prefs 写盘）', o9.set_multiplier(False), True)
check('降级链路确实停了游戏并推送了新 XML',
      'app_stop' in dev9.calls and any(c.startswith('adb_push:') for c in dev9.calls),
      str(dev9.calls))

o10, cfg10, dev10 = make_overlay()
eq('repair 恒为 False（overlay 没有部分匹配概念）', o10.repair(False), False)
set_device_xml(o10, xml_of(**{'1': 1, '2': 1, '3': 1}))
check('verify_applied 与 get_state 一致', o10.verify_applied(False) is True)
check('verify_applied 目标不符时为 False', o10.verify_applied(True) is False)

# ---------------------------------------------------------------- 7b. show()
checker.header('7b. show() 的安全前提（游戏不在跑时绝不调起 Service）')
o13, cfg13, dev13 = make_overlay()
dev13.running = False
dev13.calls.clear()
eq('游戏不在运行 -> show() 返回 False', o13.show(timeout=0.2), False)
eq('游戏不在运行时不尝试调起 Service',
   [c for c in dev13.calls if 'startservice' in c], [])

o14, cfg14, dev14 = make_overlay()
dev14.windows_dump = ''                            # 面板不在屏幕上（存活期已过）
dev14.calls.clear()
eq('面板不在 -> su 重建 Service 后面板出现 -> show() True',
   o14.show(timeout=2), True)
su_stop = [c for c in dev14.calls if 'su -c' in c and 'stopservice' in c]
su_start = [c for c in dev14.calls if 'su -c' in c and 'startservice' in c]
check('su 先 stopservice 再 startservice 各一次（重建触发 onCreate 放小球）',
      len(su_stop) == 1 and len(su_start) == 1, f'stop={su_stop} start={su_start}')

o15, cfg15, dev15 = make_overlay()
dev15.windows_dump = DUMP
dev15.calls.clear()
eq('面板已在屏幕上 -> show() 直接 True（不碰 Service）', o15.show(timeout=2), True)
eq('面板已在时不调 startservice/stopservice',
   [c for c in dev15.calls if 'startservice' in c or 'stopservice' in c], [])

# 7b-2 ★ su 通道不可用（无 root 设备）-> 退回普通 startservice 兜底；
# 被「未导出」拒绝（现场：not exported from uid 10046）-> 面板不出，
# 返回 False 交给 prefs 降级链路（停游戏 + prefs 重写）。
o16, cfg16, dev16 = make_overlay()
dev16.su_fails = True
dev16.startservice_error = 'Error: Requires permission not exported from uid 10046'
dev16.windows_dump = ''                            # 面板不在屏幕上
dev16.calls.clear()
eq('su 不可用 + 普通启动被拒 -> show() False（交给降级）',
   o16.show(timeout=0.5), False)
plain = [c for c in dev16.calls if 'am startservice' in c and 'su -c' not in c]
check('兜底普通 startservice 有尝试', len(plain) >= 1, str(plain))

# ---------------------------------------------------------------- 8. 配置读取
checker.header('8. 配置读取与边界')
o11, cfg11, dev11 = make_overlay(OverlayService='com.x.Y', OverlaySurvivalSeconds=45)
eq('service 读配置', o11.service, 'com.x.Y')
set_device_xml(o11, None)
eq('读不到 -98 时回退配置值', o11.survival_seconds, 45)
set_device_xml(o11, xml_of(**{'-98': 7}))
eq('-98 可读时以设备为准', o11.survival_seconds, 7)
set_device_xml(o11, xml_of(**{'-98': 9999}))
eq('存活秒数上限 120', o11.survival_seconds, 120)
set_device_xml(o11, xml_of(**{'-98': 1}))
eq('存活秒数下限 5', o11.survival_seconds, 5)

o12, cfg12, dev12 = make_overlay(OnKeys='', OffKeys='')
eq('OnKeys/OffKeys 都空 -> set_multiplier 返回 False', o12.set_multiplier(True), False)
eq('都空时 describe_state = unconfigured', o12.describe_state()['option'], 'unconfigured')
eq('都空时 get_state = None', o12.get_state(), None)

# ---------------------------------------------------------------- 9. prefs 读取缓存
checker.header('9. _prefs_raw 缓存（一次操作里不做重复 adb 往返）')
oc, cfgc, devc = make_overlay()
set_device_xml(oc, xml_of(**{'1': 1, '2': 1, '3': 1}))
devc.calls.clear()
oc._prefs_raw()
oc._prefs_raw()
oc.get_state()
n_cat = len([c for c in devc.calls if 'cat ' in c])
eq('TTL 内的多次读取只做一次 adb 往返', n_cat, 1)

oc._invalidate_prefs()
oc._prefs_raw()
n_cat = len([c for c in devc.calls if 'cat ' in c])
eq('失效后重新读设备', n_cat, 2)

devc.calls.clear()
oc._prefs_raw(refresh=True)
n_cat = len([c for c in devc.calls if 'cat ' in c])
eq('refresh=True 绕过缓存', n_cat, 1)

# 缓存必须真的返回同一份快照：设备在背后变了也还是旧值（这正是缓存的语义，
# 所以写盘/点击之后一定要 _invalidate_prefs）
oc2, cfg2, dev2 = make_overlay()
set_device_xml(oc2, xml_of(**{'1': 1, '2': 1, '3': 1}))
eq('缓存命中时读到的是旧快照', oc2.get_state(), False)
dev2.xml = xml_of(**{'1': 1000, '2': 1000, '3': 1000})
eq('未失效时仍读旧快照', oc2.get_state(), False)
oc2._invalidate_prefs()
eq('失效后读到新值', oc2.get_state(), True)

# ---------------------------------------------------------------- 10. wait_gone 总预算
checker.header('10. wait_gone 的整次操作总预算')
ow, cfgw, devw = make_overlay()
devw.windows_dump = DUMP                      # 面板一直在 -> 正常情况下会一直等
ow.overlay_frame = lambda: True               # 直接钉死「面板还在」
ow._wait_gone_deadline = time.time() + 0.05
_t0 = time.time()
eq('预算用完时 wait_gone 立即返回 False', ow.wait_gone(extra=60, cap=180), False)
check('预算用完时没有真的等满 survival+extra', time.time() - _t0 < 5,
      f'elapsed={time.time() - _t0:.2f}s')

ow2, cfgw2, devw2 = make_overlay()
ow2.overlay_frame = lambda: False
ow2._wait_gone_deadline = time.time() + 0.05
eq('面板已消失时预算不影响正常返回 True', ow2.wait_gone(extra=60, cap=180), True)

check('总预算是个有限的秒数（否则重试路径会累积成十几分钟阻塞）',
      isinstance(ModOverlay.WAIT_GONE_BUDGET, int) and ModOverlay.WAIT_GONE_BUDGET <= 60,
      f'WAIT_GONE_BUDGET={ModOverlay.WAIT_GONE_BUDGET}')

# ---------------------------------------------------------------- 11. 游戏未运行
checker.header('11. 游戏未运行（模拟器开着、游戏还没起来）-> 直接写 prefs')
# 用户实际踩到的场景：实例刚启动，第一个任务走到 check_then_set，但游戏还没起来。
# 旧行为：硬走悬浮窗 -> show() 拒绝（am startservice 会新起进程把游戏搞崩）
#   -> OverlayFallbackPrefs 默认关 -> 返回 False
#   -> ModHandler 回读确认「倍率还开着」-> 停机，实例一启动就自己停掉。
# 修法：游戏没在跑时直接写 prefs —— 没有运行中的进程会覆盖，零重启代价。
og, cfgg, devg = make_overlay(OverlayFallbackPrefs=False)   # 关键：降级开关是关的
set_device_xml(og, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
devg.running = False                                        # 游戏没在跑
devg.calls.clear()
eq('游戏未运行 + 降级开关关闭 -> 仍然写 prefs 并成功', og.set_multiplier(False), True)
eq('游戏未运行时不碰悬浮窗 Service',
   [c for c in devg.calls if 'service' in c], [])
eq('游戏未运行时不点击 / 不拖拽',
   [c for c in devg.calls if 'input tap' in c or 'input swipe' in c], [])
check('游戏未运行时确实推送了新的 XML',
      any(c.startswith('adb_push:') for c in devg.calls), str(devg.calls))
eq('游戏未运行时设备上的倍率已关', og.get_state(), False)

# 反向：游戏在跑时仍优先走悬浮窗；悬浮窗失败后自动降级（不再看降级开关），
# 不会因为这次改动把正常悬浮窗路径改回写 prefs
og3, cfgg3, devg3 = make_overlay(OverlayFallbackPrefs=False)
set_device_xml(og3, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
devg3.running = True
devg3.windows_dump = ''                 # 窗口不出现 -> 悬浮窗路径失败
og3._prefs = StubPrefs(result=True)
og3.show = lambda *a, **k: False        # 跳过 10s 超时，直接失败
eq('游戏在跑时悬浮窗失败 -> 自动降级走 prefs 并成功', og3.set_multiplier(False), True)
eq('自动降级把 mode 透传给 prefs', og3._prefs.calls, [False])

# 游戏未运行时 prefs 也写不进去：如实返回 False，交给 ModHandler 判断是否停机
og2, cfgg2, devg2 = make_overlay(OverlayFallbackPrefs=False)
set_device_xml(og2, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
devg2.running = False
devg2.fail_command = 'cp /data/local/tmp/_alas_mod_prefs.xml'
eq('游戏未运行时 prefs 写入失败 -> 返回 False', og2.set_multiplier(False), False)

# 例外中的例外：Alas.Error.HandleError=False 时 prefs 与 app_stop 互斥，
# 但「游戏本来就没在跑」没有 app_stop 可调用，所以互斥判定不能拦这条路 ——
# 否则 HandleError=False 的用户在冷启动时仍会被停机（同一个 bug 换个配置复现）。
og4, cfgg4, devg4 = make_overlay(OverlayFallbackPrefs=False, Error_HandleError=False)
set_device_xml(og4, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
devg4.running = False
devg4.calls.clear()
eq('游戏未运行 + HandleError 关闭 -> 仍然写 prefs 并成功',
   og4.set_multiplier(False), True)
eq('游戏未运行 + HandleError 关闭 -> 不调用 app_stop',
   [c for c in devg4.calls if c == 'app_stop'], [])
eq('游戏未运行 + HandleError 关闭 -> 设备上的倍率已关', og4.get_state(), False)

# 反向：游戏在跑 + HandleError=False + 开了降级 -> prefs 必须拒绝（互斥仍然成立），
# 由 _apply_via_prefs 把异常吞成 False，交给 ModHandler 决定停机。
og5, cfgg5, devg5 = make_overlay(OverlayFallbackPrefs=True, Error_HandleError=False)
set_device_xml(og5, xml_of(**{'1': 1000, '2': 1000, '3': 1000}))
devg5.running = True
og5.show = lambda *a, **k: False
eq('游戏在跑 + HandleError 关闭 -> prefs 拒绝写入，返回 False',
   og5.set_multiplier(False), False)
eq('游戏在跑 + HandleError 关闭 -> 不会偷偷强停游戏',
   [c for c in devg5.calls if c == 'app_stop'], [])

# ---------------------------------------------------------------- 12. 视觉矛盾告警
# 「XML 回读说已达目标，截图反解却明确不符」—— 只加 critical 日志，不改判据。
# 为什么只加日志：XML 是权威，回读达标就是成功；而 _verify_visual 的 MISMATCH
# 走 logger.attr（INFO 级），正常日志里看不见，所以这种组合值得单独抬级。
checker.header('12. 视觉矛盾告警（XML 达标 + 截图明确不符 -> critical）')
vc = ModOverlay._visual_contradiction
eq('截图失败（visual=None）不算矛盾', vc(None, {'1': 1}, []), None)
eq('空结果不算矛盾', vc({}, {'1': 1}, []), None)
eq('一致时不算矛盾', vc({'numbers': {'1': 1}, 'numbers_ok': True}, {'1': 1}, []), None)
eq('没有 numbers_ok 键（识图中途抛异常）不算矛盾',
   vc({'numbers': {'1': 1}}, {'1': 1}, []), None)
eq('numbers_ok 是 None 不算矛盾（只认明确的 False）',
   vc({'numbers_ok': None}, {'1': 1}, []), None)
eq('没配开关时 toggle_ok 缺失不算矛盾',
   vc({'numbers_ok': True}, {'1': 1, '35': False}, []), None)
check('倍率明确不符 -> 报矛盾',
      '倍率' in (vc({'numbers': {'1': 1000}, 'numbers_ok': False}, {'1': 1}, []) or ''))
check('开关明确不符 -> 报矛盾，且带上布尔键的目标值',
      '开关' in (vc({'toggle': True, 'toggle_ok': False}, {'35': False}, ['35']) or '')
      and 'False' in (vc({'toggle': True, 'toggle_ok': False}, {'35': False}, ['35']) or ''))


def apply_with_visual(visual):
    """
    跑一次 _apply 的「已达标」分支，返回 (返回值, 命中的 critical 列表)。

    把 show/expand/手势/等待全部钉死，只留「视觉矛盾」这一个变量 ——
    这样断言的就是「告警有没有打」和「判据有没有被动过」。
    """
    ov, _, dv = make_overlay()
    set_device_xml(ov, xml_of(**{'1': 1, '2': 1, '3': 1}))
    dv.running = True                       # pid 稳定，不触发 _crash_guard
    ov._pending = lambda *a, **k: (['1'], [])
    ov.show = lambda *a, **k: True
    ov.expand = lambda *a, **k: FRAME
    ov._gesture_slider = lambda *a, **k: None
    ov._alive = lambda *a, **k: True
    ov.wait_gone = lambda *a, **k: True
    ov._verify_visual = lambda *a, **k: visual
    fake_logger.records.clear()
    ret = ov._apply({'1': 1, '2': 1, '3': 1}, ['1'], [], False)
    crit = [m for lvl, m in fake_logger.records
            if lvl == 'critical' and '截图反解明确不符' in m]
    return ret, crit


ret, crit = apply_with_visual({'numbers': {'1': 1000}, 'numbers_ok': False})
eq('截图明确不符时 _apply 仍然返回 True（判据没动）', ret, True)
eq('截图明确不符时打了 1 条 critical', len(crit), 1)
check('critical 里带了可执行的排查指引',
      'mod_discover' in crit[0] and 'SLIDER_KEY_ROW' in crit[0],
      crit[0][:90] if crit else '')

ret, crit = apply_with_visual({'numbers': {'1': 1}, 'numbers_ok': True})
eq('截图一致时返回 True 且不打 critical', (ret, crit), (True, []))
ret, crit = apply_with_visual(None)
eq('截图失败时返回 True 且不打 critical（不制造噪音）', (ret, crit), (True, []))

sys.exit(checker.summary())
