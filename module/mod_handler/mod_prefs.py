"""
ModPrefs — 通过 root 直接读写改版客户端悬浮窗的 SharedPreferences。

适用：MuMu 等已 root 的环境（`su -c id` 返回 uid=0）。
不适用：未 root 的真机（会抛异常，请改用 `ui` 后端）。

为什么用 SharedPreferences：
  改版客户端的悬浮窗是 Perseus 系菜单框架（com.android.support.Menu），
  其开关状态存在 SharedPreferences 里（崩溃栈实证：
    SharedPreferencesImpl.getInt <- com.android.support.Menu$1.run）。
  功能以数字 ID 作为 key（dex 内 DEFAULT_BOOLEAN_VALUE / DEFAULT_FLOAT_VALUE / DEFAULT_INT_VALUE）。

实测补充（2026-09，MuMu 12 / 官服 JMBQ 改版）：
  - 悬浮窗是 FLAG_NOT_FOCUSABLE 的 WindowManager 窗口，不在 uiautomator2 辅助功能树里，
    所以点不到、也读不到它的控件；部分版本悬浮窗还会在数秒后自动消失。
    结论：prefs 是唯一可行的控制通道。
  - 应用对 prefs 有内存缓存，运行中改文件既可能不生效、又会在应用退出时被覆盖回去。
    因此写入前必须先停游戏；要不要马上拉起来由 ModHandler.RestartTask 决定。

注意事项：
  - 修改前必须停掉游戏进程，否则应用内存中的副本会在退出时覆盖我们的写入。
  - 写完会回读一次校验（verify_applied），避免"以为关了其实没关"。
  - key 映射通过 ModHandler.OffKeys / OnKeys 配置；用 dev_tools/mod_discover.py 自动发现。
"""
import os
import re
import tempfile
from xml.etree import ElementTree

from module.config.deep import deep_get
from module.exception import RequestHumanTakeover
from module.logger import logger


class _ModuleBaseStub:
    """PIL 不可用时的降级基类，见下面的 import 说明。"""

    def __init__(self, config=None, device=None, task=None):
        self.config = config
        self.device = device


try:
    # module.base.base 第 1 行就是 `from module.base.button import Button`，
    # 而 button.py 里有 `from PIL import ImageDraw`。
    # webui 进程为了省内存把 PIL 换成了假模块（只有 PIL.Image.Image），
    # 于是这里会 ImportError，连"只读看一眼状态"都做不到。
    # 只读路径完全用不到 ModuleBase，所以降级成空基类，让本模块照样可用。
    from module.base.base import ModuleBase
except ImportError:  # pragma: no cover - 取决于运行环境
    ModuleBase = _ModuleBaseStub

TMP_REMOTE = '/data/local/tmp/_alas_mod_prefs.xml'
DEFAULT_PACKAGE = 'com.bilibili.azurlane'
DEFAULT_PREFS_FILE = 'com.bilibili.azurlane_preferences'


def _shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _find_adb_static():
    """模块级版 find_adb：不需要实例。"""
    for candidate in ('./bin/adb/adb.exe' if os.name == 'nt' else './bin/adb/adb',
                      './toolkit/Lib/site-packages/adbutils/binaries/adb.exe',
                      './toolkit/lib/site-packages/adbutils/binaries/adb.exe',
                      '/usr/bin/adb'):
        if os.path.exists(candidate):
            return candidate
    return 'adb'


def _adb_su_static(cmd, serial=None, timeout=30):
    """模块级版 adb_su：绕过 Device 直接执行远端 root 命令（只读）。"""
    import subprocess

    argv = [_find_adb_static()]
    if serial:
        argv += ['-s', serial]
    argv += ['shell', f'su -c {_shell_quote(cmd)}']
    result = subprocess.run(argv, capture_output=True, timeout=timeout)
    return result.stdout.decode('utf-8', 'replace')


def _adb_devices_static():
    """在线设备 serial 列表。"""
    import subprocess

    try:
        result = subprocess.run([_find_adb_static(), 'devices'],
                                capture_output=True, timeout=15)
    except Exception:
        return []
    out = []
    for line in result.stdout.decode('utf-8', 'replace').splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            out.append(parts[0])
    return out


def _adb_connect_static(serial):
    """
    设备没在线就 adb connect 一次。

    模拟器掉线很常见（重启模拟器 / adb server 被别的工具重启），
    不重连的话表现是"读不到状态"，很难懂。
    """
    import subprocess

    if not serial or serial in _adb_devices_static():
        return True
    try:
        result = subprocess.run([_find_adb_static(), 'connect', serial],
                                capture_output=True, timeout=30)
    except Exception as e:
        logger.warning(f'ModPrefs: adb connect {serial} 失败: {e}')
        return False
    msg = result.stdout.decode('utf-8', 'replace').strip()
    if msg:
        logger.info(f'ModPrefs: {msg}')
    return serial in _adb_devices_static()


def _format_pref_value(value):
    """与 ModPrefs._format_value 一致。"""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _match_prefs(parsed, target):
    """parsed 是否完全等于 target（键与值都对上）。"""
    if not target:
        return False
    for k, v in target.items():
        cur = parsed.get(str(k))
        if cur is None or str(cur[1]) != _format_pref_value(v):
            return False
    return True


def _keys_detail(parsed, target):
    parts = []
    for k, v in target.items():
        cur = parsed.get(str(k))
        parts.append(f'{k}={cur[1] if cur else "(缺失)"}→目标{_format_pref_value(v)}')
    return '  '.join(parts)


def _candidate_serials(config, serial=None):
    """
    候选设备列表：显式 serial -> 配置里的 serial -> adb devices 里所有在线设备。

    为什么要逐个试：ALAS 的 Emulator_Serial 有时写成 'auto' 或用别名，
    而 adb 里可能同时挂着真机与模拟器（真机通常没 root）。
    """
    out = []
    if serial:
        out.append(serial)
    cfg_serial = str(deep_get(config, 'Alas.Emulator.Serial', default='') or '')
    if cfg_serial and cfg_serial != 'auto':
        out.append(cfg_serial)
    seen = []
    for item in out + _adb_devices_static():
        if item not in seen:
            seen.append(item)
    return seen


def describe_state_readonly(config, serial=None):
    """
    纯只读地判断当前倍率状态，给 GUI 状态栏用。

    刻意不实例化 ModPrefs（它是 ModuleBase 子类，__init__ 会做 OCR 导入、
    构造 Device，在 webui 的假 PIL 环境下必然失败），只复用下面的纯逻辑。

    Args:
        config: AzurLaneConfig 对象，或 webui 的 read_file() 返回的配置 dict
    Returns:
        dict: {'option': 'on'/'off'/'unknown'/'unconfigured', 'detail': str}
    """
    from module.mod_handler.mod_handler import parse_key_values

    data = config if isinstance(config, dict) else config.data
    package = str(deep_get(data, 'ModHandler.ModHandler.PackageName',
                           default=DEFAULT_PACKAGE) or DEFAULT_PACKAGE)
    prefs_file = str(deep_get(data, 'ModHandler.ModHandler.PrefsFile',
                              default=DEFAULT_PREFS_FILE) or DEFAULT_PREFS_FILE)
    off = parse_key_values(deep_get(data, 'ModHandler.ModHandler.OffKeys', default=''))
    on = parse_key_values(deep_get(data, 'ModHandler.ModHandler.OnKeys', default=''))

    if not off and not on:
        return {'option': 'unconfigured',
                'detail': 'OffKeys / OnKeys 未配置，请先用 dev_tools/mod_discover.py 发现 key 映射'}

    remote = f'/data/data/{package}/shared_prefs/{prefs_file}.xml'
    raw = None
    tried = []
    for candidate in _candidate_serials(data, serial):
        tried.append(candidate)
        # 掉线时先尝试重连一次，否则会静默读不到
        _adb_connect_static(candidate)
        try:
            out = _adb_su_static(f'cat {remote}', serial=candidate)
        except Exception as e:
            logger.warning(f'ModPrefs: {candidate} 读取异常: {type(e).__name__}: {e}')
            continue
        if '<map' in out:
            raw = out
            break
    if raw is None:
        return {'option': 'unknown',
                'detail': f'读不到 {remote}；已尝试设备 {tried}。'
                          f'请确认模拟器在线、包名 / PrefsFile 是否正确'}

    try:
        parsed = ModPrefs.parse(raw)
    except Exception as e:
        return {'option': 'unknown', 'detail': f'解析失败: {e}'}

    if off and _match_prefs(parsed, off):
        return {'option': 'off', 'detail': _keys_detail(parsed, off)}
    if on and _match_prefs(parsed, on):
        return {'option': 'on', 'detail': _keys_detail(parsed, on)}
    want = dict(off)
    if on:
        want.update(on)
    return {'option': 'unknown',
            'detail': '当前值不在任一目标状态上: ' + _keys_detail(parsed, want)}


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
    def restart_policy(self):
        """
        什么时候立刻重启游戏（游戏本来就没跑时不重启）。

        always         : 只要改了配置就立刻重启（默认）
        sensitive_only : 只有敏感任务（演习 / META / 共斗）关倍率时立刻重启；
                         其他任务写完把游戏留在停止状态 ——
                         ALAS 会把 GameNotRunningError 转成 Restart 任务
                         （alas.py 的 run(): self.config.task_call('Restart')），
                         所以游戏会被自己拉起来。
        never          : 只停游戏、写配置，启动完全交给 ALAS。
        """
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.RestartTask',
                            default='always') or 'always')

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

    # ------------------------------------------------------------ 只读通道（不依赖 Device）
    @staticmethod
    def find_adb():
        """找 adb 可执行文件：优先项目自带，其次 PATH 里的。"""
        for candidate in ('./bin/adb/adb.exe' if os.name == 'nt' else './bin/adb/adb',
                          './toolkit/Lib/site-packages/adbutils/binaries/adb.exe',
                          './toolkit/lib/site-packages/adbutils/binaries/adb.exe',
                          '/usr/bin/adb'):
            if os.path.exists(candidate):
                return candidate
        return 'adb'

    def adb_su(self, cmd, serial=None, timeout=30):
        """
        绕过 Device、直接用 adb 执行远端 root 命令（只读诊断用）。

        为什么需要它：webui 进程把 PIL 换成了假模块，构造 Device 会因
        `cannot import name 'ImageDraw' from 'PIL'` 失败。而读配置只需要
        `adb shell su -c cat`，没必要把整个截图/控制栈拉进来。
        写成这一层后，GUI 在没有 Device 的情况下也能读到真实状态。

        注意：只能用于读操作。写 prefs 必须走 Device（要停/启游戏）。
        """
        import subprocess

        serial = serial or str(deep_get(self.config.data, 'Alas.Emulator.Serial',
                                        default='') or '')
        argv = [self.find_adb()]
        if serial and serial != 'auto':
            argv += ['-s', serial]
        # 整条远端命令必须作为「一个」参数交给 su -c，否则 su 会误吃 -c/-f 之类的选项
        argv += ['shell', f'su -c {_shell_quote(cmd)}']
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        return result.stdout.decode('utf-8', 'replace')

    def read_raw_via_adb(self, serial=None):
        """不依赖 Device 地读取 prefs 原文，失败返回 None。"""
        try:
            out = self.adb_su(f'cat {self.remote_path}', serial=serial)
        except Exception as e:
            logger.warning(f'ModPrefs: adb 读取失败: {type(e).__name__}: {e}')
            return None
        if '<map' not in out:
            logger.warning(f'ModPrefs: adb 读到的内容不是 prefs: {out[:120]!r}')
            return None
        return out

    def read_only_describe_state(self, serial=None):
        """
        纯只读地判断当前倍率状态，供 GUI 状态栏使用（不需要 Device / root 检查走 adb）。

        Returns:
            dict: 与 describe_state() 同构
        """
        off, on = self.off_keys, self.on_keys
        if not off and not on:
            return {'option': 'unconfigured',
                    'detail': 'OffKeys / OnKeys 未配置，请先用 dev_tools/mod_discover.py 发现 key 映射'}

        # 先用配置里的 serial；读不到就在线设备里挨个试
        raw = None
        tried = []
        for candidate in self._candidate_serials(serial):
            tried.append(candidate or '(配置里的 serial)')
            raw = self.read_raw_via_adb(serial=candidate)
            if raw is not None:
                break
        if raw is None:
            return {'option': 'unknown',
                    'detail': f'读不到 {self.remote_path}；已尝试 {tried}。'
                              f'请确认设备在线（adb connect）、包名 / PrefsFile 是否正确'}
        try:
            parsed = self.parse(raw)
        except Exception as e:
            return {'option': 'unknown', 'detail': f'解析失败: {e}'}
        if off and self._match(parsed, off):
            return {'option': 'off', 'detail': self._keys_detail(parsed, off)}
        if on and self._match(parsed, on):
            return {'option': 'on', 'detail': self._keys_detail(parsed, on)}
        want = dict(off)
        if on:
            want.update(on)
        return {'option': 'unknown',
                'detail': '开关当前值不在 OffKeys/OnKeys 任一目标状态上: '
                          + self._keys_detail(parsed, want)}

    def _candidate_serials(self, serial=None):
        """
        候选设备列表：显式 serial -> 配置里的 serial -> adb devices 里所有在线设备。

        为什么要逐个试：ALAS 的 Emulator_Serial 有时写成 'auto' 或用了别名，
        而 adb 里可能有真机 + 模拟器多个设备（真机通常没有 root）。
        """
        import subprocess

        out = []
        if serial:
            out.append(serial)
        cfg_serial = str(deep_get(self.config.data, 'Alas.Emulator.Serial', default='') or '')
        if cfg_serial and cfg_serial != 'auto':
            out.append(cfg_serial)
        out.append('(all)')
        try:
            result = subprocess.run([self.find_adb(), 'devices'], capture_output=True, timeout=15)
            devices = []
            for line in result.stdout.decode('utf-8', 'replace').splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == 'device':
                    devices.append(parts[0])
        except Exception:
            devices = []
        # 去重并保持顺序
        seen = []
        for item in out:
            for d in (devices if item == '(all)' else [item]):
                if d not in seen:
                    seen.append(d)
        return seen

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
    def needs_repair(self, mode: bool):
        """
        设备是否处于「部分匹配」的中间状态，需要补写剩下几个键。

        为什么要这个：用户手动拨过悬浮窗开关时，可能只有一部分键落在目标值上
        （例如倍率已经是 1、以德服人还是 true）。此时 get_state() 判不出状态，
        老的逻辑就会在每个任务边界盲目重写全部键 + 重启一次，直到收敛。
        这里先判断是不是"差一点"，是的话只写缺的那几个键。

        保守起见：只有当目标键的值**都在** {当前值, 目标值} 之内、且**至少一个**
        已经等于目标值时才认定需要补写。否则宁可当作"完全不符"，走完整的
        停止->写入->重启流程（换一个完全不同的状态时确实需要重启）。

        Returns:
            list: 需要改写的 {key: value}；不需要补写时返回空 dict
        """
        changes = self.on_keys if mode else self.off_keys
        if not changes:
            return {}
        current = self.read_raw()
        if current is None:
            return {}
        try:
            parsed = self.parse(current)
        except Exception:
            return {}
        todo = {}
        matched = 0
        for k, v in changes.items():
            cur = parsed.get(str(k))
            if cur is None:
                continue
            cur_v = str(cur[1])
            want_v = self._format_value(v)
            if cur_v == want_v:
                matched += 1
                continue
            # 当前值既不是目标值 —— 只有它看起来像"另一个目标值"时才敢补写
            other = (self.off_keys if mode else self.on_keys).get(str(k))
            if other is not None and cur_v == self._format_value(other):
                todo[str(k)] = v
            else:
                # 完全陌生的值，不做猜测
                return {}
        if matched and todo:
            return todo
        return {}

    def set_multiplier(self, mode: bool, restart=None):
        """
        按策略把悬浮窗的倍率类开关写到目标值。

        Args:
            mode: True = 启用倍率（恢复正常），False = 关闭倍率
            restart: 写完之后要不要立刻把游戏拉起来
                     None  -> 按 ModHandler.RestartTask 的策略判断
                     True  -> 立刻重启（敏感任务，等同 AlasGG 的 gg_reset）
                     False -> 只停游戏、写入，由 ALAS 自己决定何时启动
        Returns:
            bool: 是否真的改写了配置
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

        if restart is None:
            restart = self.restart_policy == 'always'

        logger.info(f'ModPrefs: applying {todo} -> {"ON" if mode else "OFF"} '
                    f'(restart={restart})')

        # 必须停游戏：应用内存里的副本会在退出时把我们的写入覆盖回去
        was_running = self._is_game_running()
        if was_running:
            logger.info('ModPrefs: stopping game, otherwise the running instance '
                        'overwrites our write when it exits')
            self._app_stop()

        failed = False
        try:
            self.write_raw(self.build_xml(current, todo))
        except Exception:
            failed = True
            raise
        finally:
            if was_running and restart:
                logger.info('ModPrefs: restarting game so the mod re-reads prefs')
                try:
                    self._app_start()
                except Exception as e:
                    # 起不来也要把话说清楚，否则下一个任务会对着黑屏操作
                    logger.error(f'ModPrefs: 重启游戏失败({e})，请检查 ALAS 的 Error.HandleError 配置')
                    if not failed:
                        raise
            elif was_running and not failed:
                # 游戏保持关闭状态，让 ALAS 自己在需要时启动：
                # 这样比"运行中改文件"安全 —— 否则应用退出时会把改动覆盖回去。
                logger.info('ModPrefs: game left stopped on purpose, '
                            'ALAS will start it when the next task needs it')

        if failed:
            return True
        self.verify_applied(mode)
        return True

    def repair(self, mode: bool, restart=None):
        """
        只补写缺失的键（不改动已经正确的键）。

        与 set_multiplier 的区别：后者写的是完整的 OnKeys/OffKeys；
        这里只写"差一点"的那几个，代价更小，但同样需要停游戏才能落盘。

        Returns:
            bool: 是否真的写入了
        """
        todo = self.needs_repair(mode)
        if not todo:
            return False
        if not self.check_root():
            raise RuntimeError(
                'ModPrefs 需要 root，但 `su -c id` 未返回 uid=0。'
                '请改用 ModHandler.Backend = ui，或在已 root 的模拟器上运行。')
        current = self.read_raw()
        if current is None:
            raise RuntimeError(f'无法读取 {self.remote_path}，请确认包名/PrefsFile 是否正确。')

        if restart is None:
            restart = self.restart_policy == 'always'
        logger.info(f'ModPrefs: repairing {todo} -> {"ON" if mode else "OFF"} '
                    f'(restart={restart})')

        was_running = self._is_game_running()
        if was_running:
            logger.info('ModPrefs: stopping game, otherwise the running instance '
                        'overwrites our write when it exits')
            self._app_stop()
        failed = False
        try:
            self.write_raw(self.build_xml(current, todo))
        except Exception:
            failed = True
            raise
        finally:
            if was_running and restart:
                try:
                    self._app_start()
                except Exception as e:
                    logger.error(f'ModPrefs: 重启游戏失败({e})，请检查 ALAS 的 Error.HandleError 配置')
                    if not failed:
                        raise
            elif was_running and not failed:
                logger.info('ModPrefs: game left stopped on purpose, '
                            'ALAS will start it when the next task needs it')
        if not failed:
            self.verify_applied(mode)
        return True

    def verify_applied(self, mode: bool):
        """
        写完回读一次，确认设备上的值真的是目标状态。

        改配置文件这件事本身不会报错，所以必须自己回读验证 ——
        否则"以为关了其实没关"就是封号风险的来源。
        """
        try:
            state = self.get_state()
        except Exception as e:
            logger.warning(f'ModPrefs: 回读验证失败: {e}')
            return None
        want = bool(mode)
        if state is None:
            logger.warning('ModPrefs: 写入后回读不到明确状态，请用 '
                           'dev_tools/mod_handler_doctor.py 检查 key 映射')
            return None
        if state != want:
            logger.error(f'ModPrefs: 回读不一致！期望 {"ON" if want else "OFF"}，'
                         f'实际 {"ON" if state else "OFF"}')
            return False
        logger.attr('ModPrefs', f'已确认倍率 {"ON" if want else "OFF"}')
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
        """
        启动游戏，并等待登录完成。

        关键：不能只调 device.app_start() —— 那只是把 App 拉起来，
        不等登录。ALAS 紧接着接管时会对着加载界面截图，刷一堆
        "Unknown ui page" 然后 "Game page unknown" 直接崩掉。
        所以这里补上 ALAS 自己的登录处理 LoginHandler.handle_app_login()。

        注意不用 alas.py 的 restart()/LoginHandler.app_restart()：
        那个里面有 config.task_delay(server_update=True)，会把当前任务推迟到
        下一次服务器刷新（可能几小时后）。我们要的是「原地重启后继续跑当前任务」。
        """
        try:
            self.device.app_start()
        except RequestHumanTakeover as e:
            logger.warning(f'ModPrefs: device.app_start 不可用({e})，改用 monkey 启动')
            self.device.adb_shell(
                ['monkey', '-p', self.package, '-c', 'android.intent.category.LAUNCHER', '1'],
                timeout=30)
            return

        # device.config 由 alas.py 在每个任务前赋值（self.device.config = self.config）。
        # 有它说明是 ALAS 主进程里的真实设备，可以走登录流程；
        # 没有则退化为只启动（例如测试环境）。
        if getattr(self.device, 'config', None) is None:
            logger.info('ModPrefs: device has no config bound, skip login handling')
            return
        try:
            from module.handler.login import LoginHandler

            logger.info('ModPrefs: waiting for game login so Alas can continue safely')
            LoginHandler(self.device.config, device=self.device).handle_app_login()
        except Exception as e:
            # 登录失败不能把整个任务链打断：交给 ALAS 的错误处理去重启
            logger.error(f'ModPrefs: 登录等待失败({type(e).__name__}: {e})，'
                         f'Alas 会在需要时自行重启游戏')

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
