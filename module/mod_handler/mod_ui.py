"""
ModUi — 通过 uiautomator2 操作悬浮窗开关（无 root 时的兜底后端）。

适用：未 root 的真机，或不想动 SharedPreferences 的场景。
原理：打开悬浮窗 → 按文本找到开关控件 → 置为期望状态 → 关闭面板。

局限（务必知悉）：
  WindowManager 的悬浮窗如果是 FLAG_NOT_FOCUSABLE，可能不在辅助功能树里，
  此时 uiautomator2 找不到控件，需要改用坐标点击（UiTapPoints 配置）或改用 prefs 后端。

配置：
  ModHandler.UiOpenPoint    打开悬浮窗的点击坐标，形如 "x,y"；留空则假定面板已展开
  ModHandler.UiClosePoint   关闭悬浮窗的点击坐标，形如 "x,y"；留空则不关闭
  ModHandler.UiOffLabels    关闭倍率时要置为「关」的开关文本，逗号分隔
  ModHandler.UiOnLabels     恢复时要置为「开」的开关文本，逗号分隔（留空则复用 UiOffLabels）
  ModHandler.UiTapPoints    找不到控件时的坐标兜底，"文本=x,y;文本=x,y"
"""
import time

from module.base.base import ModuleBase
from module.config.deep import deep_get
from module.logger import logger


def parse_point(text):
    """"x,y" -> (x, y) 或 None"""
    if not text:
        return None
    try:
        x, y = str(text).replace('，', ',').split(',')[:2]
        return int(float(x.strip())), int(float(y.strip()))
    except Exception:
        return None


def parse_labels(text):
    if not text:
        return []
    return [s.strip() for s in str(text).replace('；', ',').replace(';', ',').split(',') if s.strip()]


def parse_tap_points(text):
    """"a=1,2;b=3,4" -> {'a': (1,2), 'b': (3,4)}"""
    result = {}
    if not text:
        return result
    for item in str(text).replace('；', ';').split(';'):
        item = item.strip()
        if not item or '=' not in item:
            continue
        name, point = item.split('=', 1)
        p = parse_point(point)
        if p:
            result[name.strip()] = p
    return result


class ModUi(ModuleBase):
    def __init__(self, config=None, device=None):
        super().__init__(config=config, device=device)
        self.config = config
        self.device = device
        self._d = None

    # ------------------------------------------------------------ 配置
    # 路径为 ModHandler.ModHandler.<参数>：ALAS 的任务名与参数组名同名时是两层
    @property
    def open_point(self):
        return parse_point(deep_get(self.config.data, 'ModHandler.ModHandler.UiOpenPoint',
                                    default=''))

    @property
    def close_point(self):
        return parse_point(deep_get(self.config.data, 'ModHandler.ModHandler.UiClosePoint',
                                    default=''))

    @property
    def off_labels(self):
        return parse_labels(deep_get(self.config.data, 'ModHandler.ModHandler.UiOffLabels',
                                     default=''))

    @property
    def on_labels(self):
        return parse_labels(deep_get(self.config.data, 'ModHandler.ModHandler.UiOnLabels',
                                     default=''))

    @property
    def tap_points(self):
        return parse_tap_points(deep_get(self.config.data, 'ModHandler.ModHandler.UiTapPoints',
                                         default=''))

    # ------------------------------------------------------------ 设备
    @property
    def d(self):
        if self._d is None:
            import uiautomator2 as u2
            self._d = u2.connect(self.device.serial)
        self.apply_wait_timeout()
        return self._d

    def apply_wait_timeout(self, connection=None):
        """把 UiWaitTimeout 应用到 u2 连接；测试会直接调用它。"""
        det = connection if connection is not None else self._d
        if det is None:
            return None
        det.wait_timeout = float(
            deep_get(self.config.data, 'ModHandler.ModHandler.UiWaitTimeout', default=5) or 5)
        return det.wait_timeout

    def _find_switch(self, label):
        """
        在悬浮窗里按文本找开关，返回 (element, kind) 或 (None, None)。
        kind 为 'switch' / 'checkbox' / 'text'
        """
        for cls, kind in (('android.widget.Switch', 'switch'),
                          ('android.widget.CheckBox', 'checkbox'),
                          ('android.widget.ToggleButton', 'switch')):
            try:
                el = self.d(className=cls, text=label)
                if el.exists:
                    return el, kind
            except Exception:
                continue
        # 文本与开关是兄弟节点时，用 xpath 找同层开关
        try:
            el = self.d.xpath(
                f'//*[@text="{label}"]/following-sibling::*['
                f'self::android.widget.Switch or self::android.widget.CheckBox][1]')
            if el.exists:
                return el, 'switch'
        except Exception:
            pass
        try:
            el = self.d.xpath(f'//*[@text="{label}"]')
            if el.exists:
                return el, 'text'
        except Exception:
            pass
        return None, None

    @staticmethod
    def _is_on(el, kind):
        """
        读取开关当前状态。

        Returns:
            True / False / None

        注意：控件不给 checked 字段时必须返回 None（未知），不能因为
        `None` 是假值就当成「已关」，否则状态未知会被误判成「已经是目标状态」
        而直接跳过，倍率就永远关不掉。
        """
        try:
            if kind in ('switch', 'checkbox'):
                checked = el.info.get('checked', None)
                if checked is None:
                    return None
                return bool(checked)
            # 文本节点：用 contentDescription 或文本里的 ON/OFF 兜底
            info = el.info
            text = (info.get('text') or '') + ' ' + (info.get('contentDescription') or '')
            if 'ON' in text.upper() and 'OFF' not in text.upper():
                return True
            if 'OFF' in text.upper():
                return False
        except Exception:
            pass
        return None

    def _toggle(self, label, want_on):
        """
        Returns:
            bool: 是否成功处理
        """
        el, kind = self._find_switch(label)
        if el is not None:
            cur = self._is_on(el, kind)
            if cur is None:
                # 状态未知，直接点一次并读回
                el.click()
                time.sleep(0.3)
                cur = self._is_on(el, kind)
                if cur == want_on:
                    logger.info(f'ModUi: `{label}` toggled to {"ON" if want_on else "OFF"}')
                    return True
                # 点反了就再点回来
                el.click()
                time.sleep(0.3)
                return self._is_on(el, kind) == want_on
            if cur == want_on:
                logger.info(f'ModUi: `{label}` already {"ON" if want_on else "OFF"}')
                return True
            el.click()
            time.sleep(0.3)
            return self._is_on(el, kind) == want_on

        # 坐标兜底：盲点，无法读回校验，只能乐观返回
        point = self.tap_points.get(label)
        if point:
            logger.info(f'ModUi: `{label}` not found as widget, tapping {point} blindly')
            self.d.click(point[0], point[1])
            time.sleep(0.3)
            return True

        logger.warning(f'ModUi: `{label}` not found and no fallback coordinate configured')
        return False

    # ------------------------------------------------------------ 对外
    def _panel(self):
        """
        悬浮窗面板的上下文管理器：进入时展开（若配了坐标），退出时收起。
        展开/收起都只是尽力而为，失败不影响开关本身的操作。
        """
        ui = self

        class _Panel:
            def __enter__(self):
                if ui.open_point:
                    logger.info(f'ModUi: opening panel at {ui.open_point}')
                    try:
                        ui.d.click(ui.open_point[0], ui.open_point[1])
                        time.sleep(0.8)
                    except Exception as e:
                        logger.warning(f'ModUi: 展开悬浮窗失败({e})，假定面板已展开')
                return self

            def __exit__(self, exc_type, exc, tb):
                if ui.close_point:
                    logger.info(f'ModUi: closing panel at {ui.close_point}')
                    try:
                        ui.d.click(ui.close_point[0], ui.close_point[1])
                        time.sleep(0.5)
                    except Exception as e:
                        logger.warning(f'ModUi: 收起悬浮窗失败({e})')
                return False

        return _Panel()

    def read_states(self):
        """
        读回所有开关的真实状态（展开面板 -> 读控件 -> 收起）。

        Returns:
            dict: {label: True/False/None}
        """
        labels = self.off_labels + [l for l in self.on_labels if l not in self.off_labels]
        if not labels:
            return {}
        states = {}
        with self._panel():
            for label in labels:
                el, kind = self._find_switch(label)
                states[label] = self._is_on(el, kind) if el is not None else None
        return states

    def get_state(self):
        """
        读回倍率开关的真实状态，供 ModHandler 判断是否需要动作。

        判定规则（与 ModPrefs 的 off/on 语义保持一致）：
          - off_labels 全部为「关」        -> False
          - on_labels 全部为「开」         -> True
          - 其余                          -> None（未知）

        OnLabels 为空时，把 OffLabels 视为「应当为开」的那组，
        即只要不是全关就当作开着。

        Returns:
            True / False / None
        """
        states = self.read_states()
        if not states:
            return None
        off = [states.get(l) for l in self.off_labels]
        on_labels = self.on_labels or self.off_labels
        on = [states.get(l) for l in on_labels]

        if off and all(v is False for v in off):
            return False
        if on and all(v is True for v in on):
            return True
        logger.warning(f'ModUi: 开关状态无法判定 {states}')
        return None

    def describe_state(self):
        """
        给 GUI 用的「当前真实状态」描述（与 ModPrefs.describe_state 同构）。

        注意：多数改版客户端的悬浮窗是 FLAG_NOT_FOCUSABLE 的 WindowManager 窗口，
        不在辅助功能树里，此时这里会返回 unknown。
        """
        if not self.off_labels and not self.on_labels:
            return {'option': 'unconfigured',
                    'detail': 'UiOffLabels / UiOnLabels 未配置，ui 后端无从下手'}
        try:
            state = self.get_state()
        except Exception as e:
            return {'option': 'unknown', 'detail': f'读取失败: {e}'}
        if state is True:
            return {'option': 'on', 'detail': f'开关 {(self.on_labels or self.off_labels)} 均为「开」'}
        if state is False:
            return {'option': 'off', 'detail': f'开关 {self.off_labels} 均为「关」'}
        return {'option': 'unknown',
                'detail': '控件读不到（悬浮窗多半不在辅助功能树里），建议改用 prefs 后端'}

    def set_multiplier(self, mode: bool, restart=None):
        """
        点悬浮窗开关。

        Args:
            restart: 仅为与 prefs 后端保持同一签名而存在，ui 后端用不上
                     （直接点开关就是即时生效，不需要重启游戏）。
        """
        labels = self.off_labels
        if mode and self.on_labels:
            labels = self.on_labels
        if not labels:
            logger.warning('ModUi: no labels configured '
                           '(ModHandler.UiOffLabels / UiOnLabels), nothing to do')
            return False

        results = {}
        with self._panel():
            for label in labels:
                results[label] = self._toggle(label, mode)

        ok = all(results.values())
        logger.attr('ModUi', f'{"ON" if mode else "OFF"} -> {results}')
        if not ok:
            logger.warning('ModUi: 部分开关未能确认，可能悬浮窗不在辅助功能树中。'
                           '建议改用 prefs 后端（需 root）或配置 UiTapPoints 坐标。')
        return ok

    def diagnose(self):
        info = {'ui_open_point': self.open_point, 'ui_close_point': self.close_point,
                'ui_off_labels': self.off_labels, 'ui_on_labels': self.on_labels,
                'ui_tap_points': self.tap_points, 'ui_found': {}, 'overlay_visible': None}
        try:
            for label in (self.off_labels + self.on_labels):
                el, kind = self._find_switch(label)
                info['ui_found'][label] = kind or 'NOT_FOUND'
            # 悬浮窗是否在窗口列表里（只能判断存在性，不能判断可见性）
            try:
                out = str(self.device.adb_shell(['dumpsys', 'window', 'windows'], timeout=15))
                info['overlay_visible'] = 'APPLICATION_OVERLAY' in out
            except Exception:
                pass
        except Exception as e:
            info['error'] = str(e)
        return info
