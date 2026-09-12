"""
ModUi 自检 —— 免 root 后端（uiautomator2 点悬浮窗）的纯逻辑验证。

不需要真机：用一个假的 uiautomator2 控件树，验证
  1. 坐标 / 标签 / 坐标兜底配置的解析
  2. 开关查找（Switch / CheckBox / ToggleButton / 文本兄弟节点 / 纯文本）
  3. 置位与读回：已经是目标状态不点；状态相反点一次；状态未知点两次兜底
  4. get_state 的三态判定（全关=False / 全开=True / 混合=None）
  5. 面板展开与收起的时机（含异常路径）
  6. 找不到控件时回落到 UiTapPoints（盲点 = 无法回读，按未达成上报，不谎报成功）

注意：真机上悬浮窗可能不在辅助功能树里（FLAG_NOT_FOCUSABLE），
那种情况只能靠 prefs 后端或坐标兜底，本测试覆盖的是「能拿到控件」时的行为。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mod_handler_testkit import Checker, FakeConfig, FakeDevice, install_stubs  # noqa: E402

install_stubs()

from module.mod_handler import mod_ui  # noqa: E402
from module.mod_handler.mod_ui import ModUi, parse_labels, parse_point, parse_tap_points  # noqa: E402

checker = Checker('ModUi 自检')
check = checker.check
eq = checker.eq

# ---------------------------------------------------------------- 1. 配置解析
checker.header('1. 配置解析')
eq('parse_point("960,540")', parse_point('960,540'), (960, 540))
eq('parse_point(" 960 ， 540 ")', parse_point(' 960 ， 540 '), (960, 540))
eq('parse_point("")', parse_point(''), None)
eq('parse_point("abc")', parse_point('abc'), None)
eq('parse_point("1.5,2.5")', parse_point('1.5,2.5'), (1, 2))

eq('parse_labels("倍攻倍防,舰船装填")', parse_labels('倍攻倍防,舰船装填'), ['倍攻倍防', '舰船装填'])
eq('parse_labels("a;b；c")', parse_labels('a;b；c'), ['a', 'b', 'c'])
eq('parse_labels("")', parse_labels(''), [])
eq('parse_tap_points("a=1,2;b=3,4")', parse_tap_points('a=1,2;b=3,4'),
   {'a': (1, 2), 'b': (3, 4)})
eq('parse_tap_points 忽略坏项', parse_tap_points('a=1,2;bad;b=x'), {'a': (1, 2)})

# ---------------------------------------------------------------- 2. 假控件
checker.header('2. 开关查找与置位（假控件树）')


class FakeWidget:
    def __init__(self, ui, label, kind, checked):
        self.ui = ui
        self.label = label
        self.kind = kind
        self.checked = checked
        self.clicks = 0

    @property
    def exists(self):
        return True

    @property
    def info(self):
        return {'text': self.label, 'checked': self.checked, 'contentDescription': ''}

    def click(self):
        self.clicks += 1
        self.ui.click_log.append(('widget', self.label))
        if self.kind in ('switch', 'checkbox', 'toggle'):
            self.checked = not self.checked

    def __repr__(self):
        return f'<{self.kind} {self.label} checked={self.checked}>'


class FakeXPath:
    def __init__(self, widget):
        self.widget = widget

    @property
    def exists(self):
        return self.widget is not None

    def click(self):
        self.widget.click()


class FakeU2:
    """足够 mod_ui 使用的假 uiautomator2 设备。"""

    def __init__(self, widgets=(), plain_text=(), point_widgets=None):
        self.widgets = list(widgets)
        self.plain_text = list(plain_text)
        self.point_widgets = point_widgets or {}
        self.click_log = []
        self.wait_timeout = 5.0

    # d(className=..., text=...)
    def __call__(self, className=None, text=None, **kwargs):
        for w in self.widgets:
            if text is not None and w.label != text:
                continue
            if className is not None and w.kind != className.split('.')[-1].lower():
                continue
            return w
        return _Missing()

    def xpath(self, expr):
        for w in self.widgets:
            if f'@text="{w.label}"' in expr:
                return FakeXPath(w)
        for w in self.plain_text:
            if f'@text="{w.label}"' in expr:
                return FakeXPath(w)
        return FakeXPath(None)

    def click(self, x, y):
        self.click_log.append(('point', x, y))
        w = self.point_widgets.get((x, y))
        if w is not None:
            w.click()


class _Missing:
    @property
    def exists(self):
        return False

    @property
    def info(self):
        return {}

    def click(self):
        raise AssertionError('should not click a missing widget')


def make_ui(widgets=(), labels=('倍攻倍防',), on_labels=None, **config_values):
    """构造一个注入了假 u2 设备的 ModUi。"""
    cfg_values = {
        'UiOffLabels': ','.join(labels),
        'UiOnLabels': ','.join(on_labels) if on_labels else '',
    }
    cfg_values.update(config_values)
    cfg = FakeConfig(**cfg_values)
    dev = FakeDevice()
    ui = ModUi(config=cfg, device=dev)
    ui._d = FakeU2(widgets)
    return ui, cfg, dev, ui._d


def switch(label, checked=True):
    return FakeWidget(None, label, 'switch', checked)


def toggle(v):
    """构造一个用于断言 checked 取反后是否等于 v 的开关。"""
    return FakeWidget(None, '倍攻倍防', 'switch', not v)


# 2.1 已经是目标状态：不点击
w = FakeWidget(None, '倍攻倍防', 'switch', False)
ui, cfg, dev, d = make_ui([w])
w.ui = d
result = ui.set_multiplier(False)
check('已关的开关不再点击', result is True and w.clicks == 0, f'clicks={w.clicks}')

# 2.2 状态相反：点一次
w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w])
w.ui = d
result = ui.set_multiplier(False)
check('开着的开关被点一次后为关', result is True and w.clicks == 1 and w.checked is False,
      f'clicks={w.clicks} checked={w.checked}')

# 2.3 恢复：再点一次
result = ui.set_multiplier(True)
check('恢复倍率点一次后为开', result is True and w.clicks == 2 and w.checked is True,
      f'clicks={w.clicks} checked={w.checked}')

# 2.4 多个开关
ws = [FakeWidget(None, '倍攻倍防', 'switch', True),
      FakeWidget(None, '舰船装填倍率', 'checkbox', True)]
ui, cfg, dev, d = make_ui(ws, labels=('倍攻倍防', '舰船装填倍率'))
for w in ws:
    w.ui = d
result = ui.set_multiplier(False)
check('多个开关全部关闭', result is True and all(w.checked is False for w in ws),
      str([w.checked for w in ws]))

# 2.5 CheckBox 也能被找到
check('CheckBox 类型被识别', ui._find_switch('舰船装填倍率')[1] == 'checkbox',
      str(ui._find_switch('舰船装填倍率')))

# 2.6 控件不给 checked：必须判定为「未知」并点一次读回，不能误判成已关
class BlindWidget(FakeWidget):
    """点击前 info 读不到 checked（模拟 FLAG_NOT_FOCUSABLE 之类的情况）。"""

    def __init__(self, ui, label, kind, checked):
        super().__init__(ui, label, kind, checked)
        self.blind = True

    @property
    def info(self):
        if self.blind:
            return {'text': self.label, 'contentDescription': ''}
        return super().info

    def click(self):
        super().click()
        self.blind = False


w = BlindWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w])
w.ui = d
eq('状态未知时 _is_on 返回 None', ui._is_on(w, 'switch'), None)
result = ui.set_multiplier(False)
check('状态未知时点一次并读回为关',
      result is True and w.clicks == 1 and w.checked is False,
      f'result={result} clicks={w.clicks} checked={w.checked}')

# 点一次后仍然读不到 -> 再点回去并判定失败
w = BlindWidget(None, '倍攻倍防', 'switch', True)
w.click = lambda: FakeWidget.click(w)   # 不解除 blind
ui, cfg, dev, d = make_ui([w])
w.ui = d
result = ui.set_multiplier(False)
check('状态始终未知时点两次并判定失败', result is False and w.clicks == 2,
      f'result={result} clicks={w.clicks}')

# 2.7 找不到控件且没配坐标兜底
ui, cfg, dev, d = make_ui([], labels=('不存在的开关',))
result = ui.set_multiplier(False)
check('找不到控件时返回 False', result is False)

# 2.8 UiTapPoints 坐标兜底：会点，但**不能**因此宣称成功
# 控件不在辅助功能树里时只能盲点，点完无法回读。以前这里乐观返回 True，
# 于是 ModHandler 认为「已确认关掉」，敏感任务带着可能还开着的倍率开打 ——
# 而且连回读复核都不会做。现在盲点一律按「未确认」上报。
w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([], labels=('倍攻倍防',), UiTapPoints='倍攻倍防=120,300')
d.point_widgets[(120, 300)] = w
w.ui = d
eq('盲点时 _toggle 返回 None（点了但无法验证）', ui._toggle('倍攻倍防', False), None)
check('坐标兜底确实点了配置的坐标', ('point', 120, 300) in d.click_log, str(d.click_log))
eq('盲点后依然读不到状态 —— 这正是「无法确认」的根据', ui.get_state(), None)

d.click_log.clear()
result = ui.set_multiplier(False)
check('盲点后 set_multiplier 如实返回 False，不谎报成功',
      result is False and ('point', 120, 300) in d.click_log,
      f'result={result} log={d.click_log}')

# 2.8b 有控件时（能回读）仍然是 True —— 别把盲点的严格性带到正常路径上
w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w])
w.ui = d
check('能拿到控件时依然确认成功并返回 True',
      ui.set_multiplier(False) is True and w.checked is False,
      f'checked={w.checked}')

# ---------------------------------------------------------------- 3. get_state
checker.header('3. get_state 三态判定')

w = FakeWidget(None, '倍攻倍防', 'switch', False)
ui, cfg, dev, d = make_ui([w])
w.ui = d
eq('全关 -> False', ui.get_state(), False)

w.checked = True
eq('全开 -> True', ui.get_state(), True)

ws = [FakeWidget(None, 'a', 'switch', True), FakeWidget(None, 'b', 'switch', False)]
ui, cfg, dev, d = make_ui(ws, labels=('a', 'b'))
for x in ws:
    x.ui = d
eq('一开一关 -> None', ui.get_state(), None)

ui, cfg, dev, d = make_ui([], labels=())
eq('没配标签 -> None', ui.get_state(), None)

# OnLabels 为空时，OffLabels 同时代表「应当为开」的那组
w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w], labels=('倍攻倍防',))
w.ui = d
eq('OnLabels 留空时开着 -> True', ui.get_state(), True)

# ---------------------------------------------------------------- 4. 面板时机
checker.header('4. 悬浮窗展开 / 收起')

w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w], UiOpenPoint='960,540', UiClosePoint='100,100')
w.ui = d
ui.set_multiplier(False)
points = [c for c in d.click_log if c[0] == 'point']
eq('先展开后收起', points, [('point', 960, 540), ('point', 100, 100)])

w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w], UiOpenPoint='960,540', UiClosePoint='')
w.ui = d
ui.set_multiplier(False)
eq('只配展开坐标时不收起', [c for c in d.click_log if c[0] == 'point'], [('point', 960, 540)])

# read_states 也要走面板
w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w], UiOpenPoint='960,540', UiClosePoint='100,100')
w.ui = d
states = ui.read_states()
eq('read_states 返回真实状态', states, {'倍攻倍防': True})
eq('read_states 同样展开并收起', [c for c in d.click_log if c[0] == 'point'],
   [('point', 960, 540), ('point', 100, 100)])

# 展开失败不应炸掉
class BrokenU2(FakeU2):
    def click(self, x, y):
        raise RuntimeError('overlay not clickable')


w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w], UiOpenPoint='960,540', UiClosePoint='100,100')
w.ui = d
ui._d = BrokenU2([w])
try:
    result = ui.set_multiplier(False)
    check('展开坐标点击失败时仍继续操作开关', w.clicks == 1, f'clicks={w.clicks}')
except Exception as e:
    check('展开坐标点击失败时仍继续操作开关', False, f'{type(e).__name__}: {e}')

# ---------------------------------------------------------------- 5. 诊断
checker.header('5. diagnose')
w = FakeWidget(None, '倍攻倍防', 'switch', True)
ui, cfg, dev, d = make_ui([w])
w.ui = d
info = ui.diagnose()
eq('diagnose 报告找到的控件', info['ui_found'], {'倍攻倍防': 'switch'})
eq('diagnose 报告 off_labels', info['ui_off_labels'], ['倍攻倍防'])

# ---------------------------------------------------------------- 6. 配置读取
checker.header('6. 配置读取')
cfg = FakeConfig(UiWaitTimeout=9, UiOpenPoint='12,34', UiOffLabels='倍攻倍防')
ui = ModUi(config=cfg, device=FakeDevice())
ui._d = FakeU2([])
eq('UiWaitTimeout 被应用到 u2 连接', ui.apply_wait_timeout(), 9.0)
eq('u2 连接对象的属性已更新', ui._d.wait_timeout, 9.0)
eq('UiOpenPoint 被解析', ui.open_point, (12, 34))
eq('未配置的 UiClosePoint 为 None', ui.close_point, None)
eq('默认 UiWaitTimeout 为 5', ModUi(
    config=FakeConfig(UiWaitTimeout=''), device=FakeDevice()
).apply_wait_timeout(FakeU2([])), 5.0)

# ---------------------------------------------------------------- 7. F-1 回归守卫
# 真实 ModuleBase 在 device=None 时会自建 Device（非 None）；ModUi 曾写成无条件
# `self.device = device` 把自建设备抹成 None（第一轮审查 F-1）。testkit 的桩已
# 用哨兵模拟该行为 —— 此断言钉住「不得抹掉」（旧写法会立刻 FAIL）。
ui_noguard = ModUi(config=FakeConfig(), device=None)
check('ModUi: device=None 时不得把（自建的）Device 抹掉（F-1 守卫）',
      ui_noguard.device is not None)

sys.exit(1 if checker.summary() else 0)
