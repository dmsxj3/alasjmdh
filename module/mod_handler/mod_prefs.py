"""
ModPrefs — 通过 root 直接读写改版客户端悬浮窗的 SharedPreferences。

适用：MuMu 等已 root 的环境（`su -c id` 返回 uid=0）。
不适用：未 root 的真机（会抛异常，请改用 `ui` 后端）。

为什么用 SharedPreferences：
  改版客户端的悬浮窗是 Perseus 系菜单框架（com.android.support.Menu），
  其开关状态存在 SharedPreferences 里（崩溃栈实证：
    SharedPreferencesImpl.getInt <- com.android.support.Menu$1.run）。
  功能以数字 ID 作为 key（dex 内 DEFAULT_BOOLEAN_VALUE / DEFAULT_FLOAT_VALUE / DEFAULT_INT_VALUE）。

注意事项：
  - 修改前必须停掉游戏进程，否则应用内存中的副本会在退出时覆盖我们的写入。
  - 应用读取该配置的时机通常是启动/打开面板时，因此改完一般需要重启游戏才生效，
    由配置项 ModHandler.RestartGame 控制（默认开启）。
  - key 映射通过 ModHandler.OffKeys / OnKeys 配置；用 dev_tools/mod_discover.py 自动发现。
"""
import os
import re
import tempfile
from xml.etree import ElementTree

from module.base.base import ModuleBase
from module.config.deep import deep_get
from module.exception import RequestHumanTakeover
from module.logger import logger

TMP_REMOTE = '/data/local/tmp/_alas_mod_prefs.xml'
DEFAULT_PACKAGE = 'com.bilibili.azurlane'
DEFAULT_PREFS_FILE = 'com.bilibili.azurlane_preferences'


def _shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


class ModPrefs(ModuleBase):
    def __init__(self, config=None, device=None):
        super().__init__(config=config, device=device)
        self.config = config
        self.device = device

    # ------------------------------------------------------------ 配置
    # 路径为 ModHandler.ModHandler.<参数>：ALAS 的任务名与参数组名同名时是两层
    @property
    def package(self):
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.PackageName',
                            default=DEFAULT_PACKAGE) or DEFAULT_PACKAGE)

    @property
    def prefs_file(self):
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.PrefsFile',
                            default=DEFAULT_PREFS_FILE) or DEFAULT_PREFS_FILE)

    @property
    def off_keys(self):
        from module.mod_handler.mod_handler import parse_key_values
        return parse_key_values(
            deep_get(self.config.data, 'ModHandler.ModHandler.OffKeys', default=''))

    @property
    def on_keys(self):
        from module.mod_handler.mod_handler import parse_key_values
        return parse_key_values(
            deep_get(self.config.data, 'ModHandler.ModHandler.OnKeys', default=''))

    @property
    def restart_game(self):
        return bool(deep_get(self.config.data, 'ModHandler.ModHandler.RestartGame', default=True))

    @property
    def remote_path(self):
        return f'/data/data/{self.package}/shared_prefs/{self.prefs_file}.xml'

    # ------------------------------------------------------------ root 辅助
    def _su(self, cmd, timeout=30):
        """
        以 root 执行命令，返回 stdout。

        注意：必须把整条命令作为「一个」字符串交给 su -c。
        若用 ['su', '-c', cmd] 这种列表形式，adb 会按空格拆开后交给远端 shell，
        su 会把命令里的 -f/-c 之类误当成自己的选项（报 su: invalid option -- f）。
        """
        quoted = _shell_quote(cmd)
        return self.device.adb_shell(f'su -c {quoted}', timeout=timeout)

    def check_root(self):
        """返回 True 表示 su 可用。"""
        try:
            out = self._su('id', timeout=15)
            return 'uid=0' in str(out)
        except Exception as e:
            logger.warning(f'ModPrefs: root check failed: {e}')
            return False

    # ------------------------------------------------------------ 读写
    def read_raw(self):
        """读取远端 prefs XML 原文，失败返回 None。"""
        out = self._su(f'cat {self.remote_path}')
        out = str(out)
        if '<map' not in out:
            logger.warning(f'ModPrefs: unexpected prefs content from {self.remote_path}')
            return None
        return out

    @staticmethod
    def parse(xml_text):
        """解析为 {'name': (type, value)} 。"""
        root = ElementTree.fromstring(xml_text)
        data = {}
        for node in root:
            name = node.get('name')
            if name is None:
                continue
            data[name] = (node.tag, node.get('value'))
        return data

    @staticmethod
    def _format_value(value):
        if isinstance(value, bool):
            return 'true' if value else 'false'
        return str(value)

    @staticmethod
    def build_xml(current_xml, changes):
        """
        在现有 XML 文本上应用 changes（{name: value}），保留其它条目与顺序。
        Returns:
            str: 新的 XML 文本
        """
        root = ElementTree.fromstring(current_xml)
        existing = {node.get('name'): node for node in root}

        for name, value in changes.items():
            value_s = ModPrefs._format_value(value)
            tag = 'boolean' if isinstance(value, bool) else (
                'int' if isinstance(value, int) else (
                    'float' if isinstance(value, float) else 'string'))
            node = existing.get(str(name))
            if node is not None:
                node.set('value', value_s)
                # 类型可能变化（如 boolean -> int），按新值重新判定
                node.tag = tag
            else:
                el = ElementTree.SubElement(root, tag)
                el.set('name', str(name))
                el.set('value', value_s)

        # ElementTree 不接受 xml 声明里的 standalone，手动拼回以贴近 Android 原生格式
        body = ElementTree.tostring(root, encoding='unicode')
        return "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n" + body + '\n'

    def write_raw(self, xml_text):
        """把 XML 写回远端，并恢复属主/权限。"""
        # 先取出原文件的属主与权限
        stat = str(self._su(f'stat -c "%u %g %a" {self.remote_path}')).strip()
        m = re.match(r'(\d+)\s+(\d+)\s+(\d+)', stat)
        uid, gid, mode = (m.group(1), m.group(2), m.group(3)) if m else ('', '', '660')

        fd, local = tempfile.mkstemp(suffix='.xml', prefix='alas_mod_prefs_')
        os.close(fd)
        try:
            with open(local, 'w', encoding='utf-8') as f:
                f.write(xml_text)
            self.device.adb_push(local, TMP_REMOTE)
            cmds = [f'cp {TMP_REMOTE} {self.remote_path}']
            if uid:
                cmds.append(f'chown {uid}:{gid} {self.remote_path}')
            cmds.append(f'chmod {mode} {self.remote_path}')
            cmds.append('restorecon ' + self.remote_path)
            cmds.append(f'rm -f {TMP_REMOTE}')
            out = self._su(' && '.join(cmds[:-1]) + ' ; ' + cmds[-1])
            logger.info(f'ModPrefs: wrote {self.remote_path} (uid={uid} mode={mode})')
            return out
        finally:
            try:
                os.remove(local)
            except OSError:
                pass

    # ------------------------------------------------------------ 对外
    def set_multiplier(self, mode: bool):
        """
        按策略把悬浮窗的倍率类开关写到目标值。
        Args:
            mode: True = 启用倍率（恢复正常），False = 关闭倍率
        """
        changes = self.on_keys if mode else self.off_keys
        if not changes:
            which = 'OnKeys' if mode else 'OffKeys'
            logger.warning(f'ModPrefs: ModHandler.{which} is empty, nothing to write. '
                           f'请先用 dev_tools/mod_discover.py 发现 key 映射并填入配置。')
            return False

        if not self.check_root():
            raise RuntimeError(
                'ModPrefs 需要 root，但 `su -c id` 未返回 uid=0。'
                '请改用 ModHandler.Backend = ui，或在已 root 的模拟器上运行。')

        current = self.read_raw()
        if current is None:
            raise RuntimeError(f'无法读取 {self.remote_path}，请确认包名/PrefsFile 是否正确。')

        parsed = self.parse(current)
        todo = {}
        for k, v in changes.items():
            cur = parsed.get(str(k))
            if cur is None or str(cur[1]) != self._format_value(v):
                todo[str(k)] = v
        if not todo:
            logger.info(f'ModPrefs: already at target state ({changes}), skip')
            return False

        logger.info(f'ModPrefs: applying {todo} -> {"ON" if mode else "OFF"}')

        # 必须停游戏，否则应用内存里的副本会在退出时覆盖我们的写入
        was_running = self._is_game_running()
        if was_running:
            logger.info('ModPrefs: stopping game to apply prefs safely')
            self._app_stop()

        failed = False
        try:
            self.write_raw(self.build_xml(current, todo))
        except Exception:
            failed = True
            raise
        finally:
            if was_running and self.restart_game:
                logger.info('ModPrefs: restarting game so the mod re-reads prefs')
                try:
                    self._app_start()
                except Exception as e:
                    # 起不来也要把话说清楚，否则下一个任务会对着黑屏操作
                    logger.error(f'ModPrefs: 重启游戏失败({e})，请检查 ALAS 的 Error.HandleError 配置')
                    if not failed:
                        raise
            elif was_running:
                logger.warning('ModPrefs: 已停游戏但 ModHandler.RestartGame 关闭，'
                               '游戏需要手动重新启动才能重新读取配置')

        return True

    def _is_game_running(self):
        try:
            out = str(self.device.adb_shell(['pidof', self.package], timeout=10))
            return bool(out.strip())
        except Exception:
            return False

    def _app_stop(self):
        """
        停游戏。

        Device.app_stop 在 Alas.Error.HandleError 关闭时会抛 RequestHumanTakeover，
        但这里停游戏只是为了安全写入 prefs，不是「重启游戏」的业务语义，
        因此这种情况下退化为直接用 adb 强停，避免把整个任务链打断。
        """
        try:
            self.device.app_stop()
        except RequestHumanTakeover as e:
            logger.warning(f'ModPrefs: device.app_stop 不可用({e})，改用 am force-stop')
            self.device.adb_shell(['am', 'force-stop', self.package], timeout=15)

    def _app_start(self):
        """启动游戏；同样绕开 Device.app_start 对 Error.HandleError 的依赖。"""
        try:
            self.device.app_start()
        except RequestHumanTakeover as e:
            logger.warning(f'ModPrefs: device.app_start 不可用({e})，改用 monkey 启动')
            self.device.adb_shell(
                ['monkey', '-p', self.package, '-c', 'android.intent.category.LAUNCHER', '1'],
                timeout=30)

    def _match(self, parsed, target):
        """parsed 是否完全等于 target（键与值都对上）。"""
        if not target:
            return False
        for k, v in target.items():
            cur = parsed.get(str(k))
            if cur is None or str(cur[1]) != self._format_value(v):
                return False
        return True

    def describe_state(self):
        """
        给 GUI 用的「当前真实状态」描述。

        Returns:
            dict: {
                'option': 'on' / 'off' / 'unknown' / 'unconfigured',
                'detail': 人类可读的细节（当前值 vs 目标值）,
            }
        """
        off, on = self.off_keys, self.on_keys
        if not off and not on:
            return {'option': 'unconfigured',
                    'detail': 'OffKeys / OnKeys 未配置，请先用 dev_tools/mod_discover.py 发现 key 映射'}
        if not self.check_root():
            return {'option': 'unknown', 'detail': '设备无 root，读不到悬浮窗配置'}
        raw = self.read_raw()
        if raw is None:
            return {'option': 'unknown',
                    'detail': f'读不到 {self.remote_path}，请确认包名 / PrefsFile'}
        try:
            parsed = self.parse(raw)
        except Exception as e:
            return {'option': 'unknown', 'detail': f'解析失败: {e}'}

        want = dict(off)
        if on:
            want.update(on)

        if off and self._match(parsed, off):
            return {'option': 'off', 'detail': self._keys_detail(parsed, off)}
        if on and self._match(parsed, on):
            return {'option': 'on', 'detail': self._keys_detail(parsed, on)}
        return {'option': 'unknown',
                'detail': '开关当前值不在 OffKeys/OnKeys 任一目标状态上: '
                          + self._keys_detail(parsed, want)}

    def _keys_detail(self, parsed, target):
        parts = []
        for k, v in target.items():
            cur = parsed.get(str(k))
            parts.append(f'{k}={cur[1] if cur else "(缺失)"}→目标{self._format_value(v)}')
        return '  '.join(parts)

    def get_state(self):
        """
        读取设备上真实的倍率开关状态。
        比状态缓存可靠：用户手动动过悬浮窗时也能正确发现。
        Returns:
            True  = 倍率开启
            False = 倍率关闭
            None  = 无法判断（未 root / 读不到 / OffKeys 与 OnKeys 都没配）
        """
        off, on = self.off_keys, self.on_keys
        if not off and not on:
            return None
        try:
            if not self.check_root():
                return None
            raw = self.read_raw()
            if raw is None:
                return None
            parsed = self.parse(raw)
            if off and self._match(parsed, off):
                return False
            if on and self._match(parsed, on):
                return True
            return None
        except Exception as e:
            logger.warning(f'ModPrefs: get_state failed: {e}')
            return None

    def diagnose(self):
        info = {
            'package': self.package,
            'prefs_file': self.prefs_file,
            'remote_path': self.remote_path,
            'root': None,
            'exists': None,
            'off_keys': self.off_keys,
            'on_keys': self.on_keys,
            'current_values': None,
            'game_running': None,
        }
        try:
            info['root'] = self.check_root()
            if info['root']:
                raw = self.read_raw()
                info['exists'] = raw is not None
                if raw:
                    parsed = self.parse(raw)
                    keys = set(str(k) for k in list(self.off_keys) + list(self.on_keys))
                    info['current_values'] = {k: parsed.get(k) for k in sorted(keys)}
                info['game_running'] = self._is_game_running()
        except Exception as e:
            info['error'] = str(e)
        return info
