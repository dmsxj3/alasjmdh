import json
import os
import re
import time

from module.base.template import Template
from module.logger import logger

CONFIG_FILE = './config/jmbq.json'
STATE_FILE = './config/jmbq_state.json'

# 无战斗 / 不会进入战斗画面的任务，不做倍率操作
UTILITY_TASKS = [
    'restart', 'goto_main', 'alas', 'daemon', 'opsi_daemon',
    'benchmark', 'game_manager', 'azur_lane_uncensored', 'log_res',
]

DEFAULT_CONFIG = {
    'enabled': True,
    'package': 'com.bilibili.AzurLane',
    'overlay_service': 'com.android.support.Launcher',
    # prefs 键 -98：悬浮窗存活秒数（<5 会被 mod 重置为 20）。
    # 游戏未运行时由本模块 root 写入，下次游戏启动生效。
    'survival_seconds': 8,
    # 敏感任务（倍率=关）：演习 / 共斗 / META；其余任务倍率=开
    'sensitive_tasks': [
        'exercise',
        'raid', 'raid_daily',
        'coalition', 'coalition_sp',
        'opsi_ash_assist', 'opsi_ash_beacon',
    ],
}

# 模板文件（由 bin/jmbq_collect.py 采集流程生成，放入 ./assets/jmbq/）
TEMPLATE_DIR = './assets/jmbq'
TEMPLATES = ['OVERLAY_EXPANDED', 'OVERLAY_COLLAPSED', 'SWITCH_ON', 'SWITCH_OFF']

_warn_templates_missing = False


def _read_json(path, default):
    try:
        with open(path, mode='r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning(f'JMBQ: read {path} failed: {e}')
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, mode='w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class JMBQMultiplier:
    def __init__(self, device):
        self.device = device

    def _load_templates(self):
        """
        Returns:
            dict[str, Template]: name -> Template, None if any template missing.
        """
        global _warn_templates_missing
        result = {}
        for name in TEMPLATES:
            path = os.path.join(TEMPLATE_DIR, f'{name}.png')
            if not os.path.exists(path):
                if not _warn_templates_missing:
                    logger.warning(
                        f'JMBQ: template {path} not found. '
                        f'Run `toolkit\\python.exe bin\\jmbq_collect.py` to collect overlay screenshots, '
                        f'crop the buttons and save them under {TEMPLATE_DIR}/')
                    _warn_templates_missing = True
                return None
            result[name] = Template(file=path)
        return result

    def _show_overlay(self, cfg):
        """
        Start Launcher service to re-show the floating menu.
        MuMu adb is root (uid 0), which is allowed to start non-exported components.
        """
        pkg = cfg['package']
        service = cfg['overlay_service']
        try:
            self.device.adb_shell(['am', 'startservice', '-n', f'{pkg}/{service}'])
            logger.info('JMBQ: overlay service started')
            return True
        except Exception as e:
            logger.warning(
                f'JMBQ: failed to start overlay service ({e}). '
                f'If SecurityException: emulator adb is not root, start the overlay manually and retry.')
            return False

    def _write_survival(self, cfg):
        """
        Write prefs key -98 (overlay survival seconds) via root, so the overlay
        dies shortly after the multiplier is clicked. Only effective when the
        game process is not running (SharedPreferences cache would overwrite it).
        """
        pkg = cfg['package']
        sec = int(cfg.get('survival_seconds', 8))
        if sec < 5:
            logger.warning(f'JMBQ: survival_seconds={sec} < 5 would be reset by mod to 20, skip')
            return
        xml = f'/data/data/{pkg}/shared_prefs/{pkg}_preferences.xml'
        # Game running: skip (it would rewrite the file on exit)
        try:
            pid = self.device.adb_shell(['pidof', pkg]) or ''
            if str(pid).strip():
                logger.info('JMBQ: game is running, skip prefs write (effective after next game restart)')
                return
        except Exception:
            pass
        # Read current xml
        content = None
        for cmd in (f'su -c "cat {xml}"', f'cat {xml}'):
            try:
                out = self.device.adb_shell(cmd)
                out = str(out)
                if '<map' in out:
                    content = out
                    break
            except Exception:
                continue
        if content is None:
            logger.info('JMBQ: prefs xml not readable (no root?), overlay keeps default survival time')
            return
        new_content, n = re.subn(
            r'(<int name="-98" value=")\d+(")',
            rf'\g<1>{sec}\g<2>', content)
        if n == 0:
            logger.info('JMBQ: key -98 not found in prefs xml, overlay keeps default survival time')
            return
        if new_content == content:
            return
        # Push modified xml back with correct ownership
        try:
            tmp = '/data/local/tmp/jmbq_prefs.xml'
            self.device.adb_shell(['rm', '-f', tmp])
            # adb push via local temp file
            local = os.path.abspath('./config/jmbq_prefs.xml.tmp')
            with open(local, 'w', encoding='utf-8', newline='') as f:
                f.write(new_content)
            self.device.adb_command(['push', local, tmp], timeout=10)
            uid_out = str(self.device.adb_shell(f'dumpsys package {pkg} | grep -o "userId=[0-9]*" | head -1'))
            uid = re.search(r'userId=(\d+)', uid_out)
            chown = f' && chown {uid.group(1)}:{uid.group(1)} {xml} && chmod 660 {xml}' if uid else ''
            ok = False
            # Path 1: root adb (MuMu), commands run directly
            # Path 2: su -c (single quoted, no special chars in cp/chown)
            steps = [
                f'cp {tmp} {xml}{chown}',
                f'su -c "cp {tmp} {xml}"' + (f' && su -c "chown {uid.group(1)}:{uid.group(1)} {xml}" && su -c "chmod 660 {xml}"' if chown else ''),
            ]
            for cmd in steps:
                try:
                    self.device.adb_shell(cmd)
                    check = str(self.device.adb_shell(f'cat {xml}' if not chown or cmd == steps[0] else f'su -c "cat {xml}"'))
                    if '-98' in check:
                        logger.info(f'JMBQ: prefs -98 set to {sec}s, effective after next game start')
                        ok = True
                        break
                except Exception:
                    continue
            if not ok:
                logger.warning('JMBQ: prefs write did not verify, overlay keeps default survival time')
            self.device.adb_shell(['rm', '-f', tmp])
            os.remove(local)
        except Exception as e:
            logger.warning(f'JMBQ: write prefs failed: {e}')

    def _match_center(self, tpl, image):
        buttons = tpl.match_multi(image)
        if not buttons:
            return None
        b = buttons[0]
        area = b.button
        return (area[0] + area[2]) // 2, (area[1] + area[3]) // 2

    def _wait_overlay(self, cfg, tpls, appear=True, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                img = self.device.screenshot()
            except Exception as e:
                logger.warning(f'JMBQ: screenshot failed: {e}')
                time.sleep(1)
                continue
            found = any(tpl.match(img) for name, tpl in tpls.items()
                        if name in ('OVERLAY_EXPANDED', 'OVERLAY_COLLAPSED'))
            if found == appear:
                return True
            time.sleep(0.5)
        return False

    def _detect_state(self, tpls, image):
        """
        Returns:
            tuple[str, tuple]: state ('on'/'off'/'unknown') and click point.
        """
        for state in ('on', 'off'):
            tpl = tpls[f'SWITCH_{state.upper()}']
            pos = self._match_center(tpl, image)
            if pos is not None:
                return state, pos
        return 'unknown', None

    def ensure(self, task_command, cfg):
        task = str(task_command).lower()
        if task in UTILITY_TASKS:
            return
        sensitive = set(cfg.get('sensitive_tasks', DEFAULT_CONFIG['sensitive_tasks']))
        target_on = task not in sensitive

        state = _read_json(STATE_FILE, {'multiplier_on': None})
        current = state.get('multiplier_on')
        if current is not None and current is target_on:
            return

        logger.info(f'JMBQ: task `{task}` requires multiplier {"ON" if target_on else "OFF"} (current: {current})')

        tpls = self._load_templates()
        if tpls is None:
            return

        self._write_survival(cfg)
        if not self._show_overlay(cfg):
            return
        if not self._wait_overlay(cfg, tpls, appear=True, timeout=8):
            logger.warning('JMBQ: overlay did not appear in 8s, skip this task')
            return

        # Expand menu if collapsed
        img = self.device.screenshot()
        if not tpls['OVERLAY_EXPANDED'].match(img):
            pos = self._match_center(tpls['OVERLAY_COLLAPSED'], img)
            if pos is None:
                logger.warning('JMBQ: overlay neither expanded nor collapsed detected, skip')
                return
            self.device.click(*pos)
            time.sleep(1)
            img = self.device.screenshot()
            if not tpls['OVERLAY_EXPANDED'].match(img):
                logger.warning('JMBQ: failed to expand overlay, skip')
                return

        # Detect and switch
        state_now, pos = self._detect_state(tpls, img)
        if state_now == 'unknown':
            logger.warning('JMBQ: switch state unknown (templates outdated or UI changed), skip')
            return
        if state_now != target_on:
            self.device.click(*pos)
            logger.info(f'JMBQ: clicked multiplier switch -> {"ON" if target_on else "OFF"}')
            time.sleep(1)
            img = self.device.screenshot()
            state_now, _ = self._detect_state(tpls, img)
            if state_now != target_on:
                logger.warning(f'JMBQ: switch state verify failed (got {state_now}), state marked unknown')
                _write_json(STATE_FILE, {'multiplier_on': None, 'task': task})
                return
        _write_json(STATE_FILE, {'multiplier_on': target_on, 'task': task})

        # Clean up: wait until overlay dies by itself (-98 seconds)
        timeout = int(cfg.get('survival_seconds', 8)) + 5
        gone = self._wait_overlay(cfg, tpls, appear=False, timeout=timeout)
        if gone:
            logger.info('JMBQ: overlay gone, screen clean')
        else:
            logger.warning(f'JMBQ: overlay still visible after {timeout}s (will die on its own)')


def jmbq_ensure_multiplier(device, task_command):
    """
    Entry hook for alas.py loop(). Never raises.
    """
    try:
        cfg = _read_json(CONFIG_FILE, DEFAULT_CONFIG)
        if not cfg.get('enabled', True):
            return
        JMBQMultiplier(device).ensure(task_command, cfg)
    except Exception as e:
        logger.warning(f'JMBQ: ensure_multiplier error: {e}')
