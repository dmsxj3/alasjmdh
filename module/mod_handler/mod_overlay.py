"""
ModOverlay — 悬浮窗直点后端（零重启）。

原理（基于 MOD_MENU 3.4.0 smali 逆向）：
  - 悬浮窗由 UnityPlayerActivity 构造时 Main.Start() 创建（Activity 窗口，免悬浮权限），
    N 秒后 Menu$1 removeView 自杀，prefs 键 -98 = 存活秒数（<5 重置 20）。
  - manifest 里注册了无人调用的备用组件 Launcher service（exported=false，root uid 0 可调起）：
    Launcher.onCreate() -> new Menu -> SetWindowManagerWindowService -> ShowMenu，
    即 am startservice 一次 = 悬浮窗重新出现。
  - 点击"以德服人"开关 = mod 按钮 listener 同时写 prefs 并调 native 改内存，立即生效，
    无需重启游戏 —— 这是与 ModPrefs（写 XML + 重启）的本质区别。

流程：
  am startservice 调起悬浮窗 -> 截图识图（模板匹配）找到开关 -> 判断当前视觉状态
  -> 与目标不符才点击 -> 复验 -> 等待悬浮窗按 -98 秒自杀清场。

模板（assets/mod_handler/，由悬浮窗截图裁剪）：
  OVERLAY_EXPANDED.png   展开面板锚点
  OVERLAY_COLLAPSED.png  收起小球锚点
  SWITCH_ON.png          "以德服人"开关 = 开
  SWITCH_OFF.png         "以德服人"开关 = 关
"""
import os
import time

from module.base.template import Template
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

_warn_missing = False


class ModOverlay(ModuleBase):
    def __init__(self, config=None, device=None):
        super().__init__(config=config, device=device)
        self.config = config
        self.device = device
        self._prefs = None
        self._tpl = None

    # ------------------------------------------------------------ 配置
    @property
    def package(self):
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.PackageName',
                            default='com.bilibili.AzurLane') or 'com.bilibili.AzurLane')

    @property
    def service(self):
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.OverlayService',
                            default='com.android.support.Launcher') or 'com.android.support.Launcher')

    @property
    def survival_seconds(self):
        try:
            return int(deep_get(self.config.data, 'ModHandler.ModHandler.OverlaySurvivalSeconds',
                                default=8) or 8)
        except (TypeError, ValueError):
            return 8

    @property
    def prefs_reader(self):
        """复用 ModPrefs 读 XML（悬浮窗点击时 mod 自身会同步写 prefs，
        因此 XML 状态 = 最近一次点击的状态 = 内存状态）。"""
        if self._prefs is None:
            from module.mod_handler.mod_prefs import ModPrefs
            self._prefs = ModPrefs(config=self.config, device=self.device)
        return self._prefs

    # ------------------------------------------------------------ 模板
    def templates(self):
        global _warn_missing
        if self._tpl is not None:
            return self._tpl
        result = {}
        for name in TEMPLATES:
            path = os.path.join(TEMPLATE_DIR, f'{name}.png')
            if not os.path.exists(path):
                if not _warn_missing:
                    logger.warning(
                        f'ModOverlay: template {path} not found. '
                        f'Capture overlay screenshots and crop the four buttons into {TEMPLATE_DIR}/ '
                        f'(OVERLAY_EXPANDED / OVERLAY_COLLAPSED / SWITCH_ON / SWITCH_OFF).')
                _warn_missing = True
                return None
            result[name] = Template(file=path)
        self._tpl = result
        return result

    # ------------------------------------------------------------ 基础动作
    def _match_center(self, tpl, image):
        buttons = tpl.match_multi(image)
        if not buttons:
            return None
        area = buttons[0].button
        return (area[0] + area[2]) // 2, (area[1] + area[3]) // 2

    def _overlay_visible(self, tpls, image):
        for name in ('OVERLAY_EXPANDED', 'OVERLAY_COLLAPSED'):
            if tpls[name].match(image):
                return True
        return False

    def show_overlay(self):
        """
        程序化调起悬浮窗（Launcher service）。
        实测（MOD_MENU 3.4.0 @ MuMu 12）：
          - adbd 非 root 时 am startservice 会被拒（exported=false），需要 `adb root`
            或走 `su -c`（MuMu 内置 root）；
          - service 存活时 startservice 只触发 onStartCommand，悬浮窗不会重建，
            必须先 stopservice（onDestroy 为空、无副作用）再 startservice。
        """
        target = f'{self.package}/{self.service}'

        def _run(cmd):
            try:
                self.device.adb_shell(cmd)
                return True
            except Exception:
                # adbd 非 root：回退 su -c
                try:
                    self.device.adb_shell('su -c "' + ' '.join(cmd) + '"')
                    return True
                except Exception as e:
                    logger.warning(f'ModOverlay: `{" ".join(cmd)}` failed: {e}')
                    return False

        ok = _run(['am', 'stopservice', '-n', target])
        ok = _run(['am', 'startservice', '-n', target]) and ok
        if ok:
            logger.attr('ModOverlay', f'overlay restarted ({self.service})')
        return ok

    def _write_survival(self):
        """游戏未运行时 root 改 prefs -98 = 存活秒数，缩短悬浮窗清场时间。
        游戏运行中写会被进程退出时的回写覆盖，因此跳过。"""
        sec = self.survival_seconds
        if sec < 5:
            return
        pkg = self.package
        xml = f'/data/data/{pkg}/shared_prefs/{pkg}_preferences.xml'
        try:
            pid = str(self.device.adb_shell(['pidof', pkg]) or '').strip()
            if pid:
                return
            content = ''
            for cat in (f'cat {xml}', f'su -c "cat {xml}"'):
                try:
                    content = str(self.device.adb_shell(cat))
                    if '<map' in content:
                        break
                except Exception:
                    continue
            if '<map' not in content:
                return
            import re
            new_content, n = re.subn(r'(<int name="-98" value=")\d+(")', rf'\g<1>{sec}\g<2>', content)
            if n == 0 or new_content == content:
                return
            self.prefs_reader.write_raw(new_content)
            logger.info(f'ModOverlay: prefs -98 set to {sec}s (effective after next game start)')
        except Exception as e:
            logger.info(f'ModOverlay: shorten survival skipped: {type(e).__name__}')

    # ------------------------------------------------------------ 状态
    def get_state(self):
        """
        读设备真实倍率状态。悬浮窗不在屏幕上时识图不可用，
        因此以 prefs XML 为准（点击时 mod 同步落盘，见类注释）。
        Returns:
            True = 倍率开 / False = 倍率关 / None = 未知
        """
        try:
            return self.prefs_reader.get_state()
        except Exception as e:
            logger.warning(f'ModOverlay: read state failed: {e}')
            return None

    def describe_state(self):
        keys = self.prefs_reader.keys_configured if hasattr(self.prefs_reader, 'keys_configured') else True
        if not keys:
            return {'option': 'unconfigured', 'detail': 'OffKeys / OnKeys 未配置，无法判定状态'}
        try:
            state = self.get_state()
        except Exception as e:
            return {'option': 'unknown', 'detail': f'读取失败: {e}'}
        if state is True:
            return {'option': 'on', 'detail': '倍率已开启（读悬浮窗 prefs XML）'}
        if state is False:
            return {'option': 'off', 'detail': '倍率已关闭（读悬浮窗 prefs XML）'}
        return {'option': 'unknown', 'detail': '设备无 root 或读不到配置'}

    # ------------------------------------------------------------ 主操作
    def _switch_action(self, tpls, image, mode):
        """
        在展开面板上定位开关。
        Returns:
            (need_click, position)
        """
        tpl = tpls['SWITCH_ON'] if mode else tpls['SWITCH_OFF']
        pos = self._match_center(tpl, image)
        if pos is not None:
            return False, pos
        # 当前视觉态与目标不符 -> 需要点击对面状态的按钮
        other = tpls['SWITCH_OFF'] if mode else tpls['SWITCH_ON']
        pos = self._match_center(other, image)
        if pos is not None:
            return True, pos
        return False, None

    def set_multiplier(self, mode: bool, restart=None):
        """
        调起悬浮窗并点击"以德服人"开关，native 直改内存立即生效，零重启。
        Args:
            mode: True = 开倍率, False = 关倍率
            restart: 兼容签名，overlay 后端不使用（天然零重启）
        Returns:
            bool: 是否确认完成了切换（无需切换也算 True）
        """
        tpls = self.templates()
        if tpls is None:
            return False

        self._write_survival()
        if not self.show_overlay():
            return False

        # 等悬浮窗出现
        deadline = time.time() + 10
        img = None
        while time.time() < deadline:
            try:
                img = self.device.screenshot()
            except Exception as e:
                logger.warning(f'ModOverlay: screenshot failed: {e}')
                time.sleep(1)
                continue
            if self._overlay_visible(tpls, img):
                break
            time.sleep(0.5)
        else:
            logger.warning('ModOverlay: overlay did not appear in 10s')
            return False

        # 收起态则展开
        if not tpls['OVERLAY_EXPANDED'].match(img):
            pos = self._match_center(tpls['OVERLAY_COLLAPSED'], img)
            if pos is None:
                logger.warning('ModOverlay: overlay neither expanded nor collapsed matched')
                return False
            self.device.click(*pos)
            time.sleep(1)
            img = self.device.screenshot()
            if not tpls['OVERLAY_EXPANDED'].match(img):
                logger.warning('ModOverlay: failed to expand overlay')
                return False

        # 定位开关并判断
        need_click, pos = self._switch_action(tpls, img, mode)
        if pos is None:
            logger.warning('ModOverlay: multiplier switch not found on overlay (templates outdated?)')
            return False
        if not need_click:
            logger.attr('ModOverlay', f'multiplier already {"ON" if mode else "OFF"}')
        else:
            self.device.click(*pos)
            logger.info(f'ModOverlay: clicked multiplier switch -> {"ON" if mode else "OFF"}')
            time.sleep(1)
            img = self.device.screenshot()
            need_click2, _ = self._switch_action(tpls, img, mode)
            if need_click2:
                logger.warning('ModOverlay: switch state verify failed after click')
                return False

        # 清场：等悬浮窗按 -98 秒自杀，确保不干扰后续任务识图
        timeout = self.survival_seconds + 5
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if not self._overlay_visible(tpls, self.device.screenshot()):
                    logger.attr('ModOverlay', 'overlay gone, screen clean')
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            logger.warning(f'ModOverlay: overlay still visible after {timeout}s (dies on its own)')
        return True

    def repair(self, mode: bool, restart=None):
        """与 set_multiplier 相同（overlay 后端没有部分匹配概念，XML 状态即全量状态）。"""
        return self.set_multiplier(mode, restart=restart)

    def verify_applied(self, mode: bool):
        return self.get_state() is mode

    def diagnose(self):
        info = {
            'backend': 'overlay',
            'overlay_service': self.service,
            'survival_seconds': self.survival_seconds,
            'templates': {name: os.path.exists(os.path.join(TEMPLATE_DIR, f'{name}.png'))
                          for name in TEMPLATES},
        }
        try:
            info['prefs'] = self.prefs_reader.diagnose()
        except Exception as e:
            info['prefs_error'] = str(e)
        try:
            out = str(self.device.adb_shell(['pidof', self.package]) or '').strip()
            info['game_running'] = bool(out)
        except Exception:
            info['game_running'] = None
        return info
