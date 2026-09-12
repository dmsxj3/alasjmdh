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
    因此写入前必须先停游戏；写完不主动拉起，游戏保持关闭，
    ALAS 下一步截图会收到 GameNotRunningError 并自动排一个 Restart 任务把它拉起来。

注意事项：
  - 修改前必须停掉游戏进程，否则应用内存中的副本会在退出时覆盖我们的写入。
  - 写完会回读一次校验（verify_applied），回读不一致按失败处理 ——
    避免"以为关了其实没关"。
  - key 映射通过 ModHandler.OffKeys / OnKeys 配置；用 dev_tools/mod_discover.py 自动发现。
"""
import os
import re
import tempfile
from xml.etree import ElementTree

from module.config.deep import deep_get
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
# write_raw 的写入成功标记：adb_shell 只回 stdout，cp/chmod 失败时 stderr 不会
# 出现在返回值里，没有这个标记就无法区分「写成功但没输出」和「根本没写进去」。
WRITE_OK_MARK = '__ALAS_PREFS_OK__'
DEFAULT_PACKAGE = 'com.bilibili.azurlane'
DEFAULT_PREFS_FILE = 'com.bilibili.azurlane_preferences'


def _shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _find_adb_static():
    """模块级版 find_adb：不需要实例。"""
    # 优先用 __file__ 拼出来的仓库内绝对路径 —— 相对路径('./toolkit/...')依赖 cwd，
    # 换个 cwd（dev_tools chdir、从 Electron 启动）会静默退回 PATH 上的 adb。
    here = os.path.dirname(os.path.abspath(__file__))          # .../module/mod_handler
    repo = os.path.abspath(os.path.join(here, '..', '..'))     # 仓库根
    candidates = [
        os.path.join(repo, 'toolkit', 'Lib', 'site-packages', 'adbutils', 'binaries', 'adb.exe'),
        os.path.join(repo, 'toolkit', 'lib', 'site-packages', 'adbutils', 'binaries', 'adb.exe'),
        './bin/adb/adb.exe' if os.name == 'nt' else './bin/adb/adb',
        './toolkit/Lib/site-packages/adbutils/binaries/adb.exe',
        './toolkit/lib/site-packages/adbutils/binaries/adb.exe',
        '/usr/bin/adb',
    ]
    for candidate in candidates:
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
        # device=None 时 ModuleBase 已经按 config 建好了 Device（它会去连模拟器），
        # 无条件覆盖会把 self.device 变成 None，之后每个 adb 调用都会
        # AttributeError: 'NoneType' has no attribute 'adb_shell'。
        if device is not None:
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

    # root 状态在一次进程生命周期内不会变，做类级缓存：ModHandler/ModOverlay
    # 每个任务边界都新建实例，实例级缓存无效；这里省掉每个边界的 su -c id
    # 往返（实测 100~500ms/次）。失败（False）不缓存 —— root 断了恢复后要能自愈。
    _ROOT_OK = None

    def check_root(self):
        """返回 True 表示 su 可用（进程内缓存，成功后不再重复探测）。"""
        if ModPrefs._ROOT_OK is True:
            return True
        try:
            out = self._su('id', timeout=15)
            ok = 'uid=0' in str(out)
            if ok:
                ModPrefs._ROOT_OK = True
            else:
                logger.warning(f'ModPrefs: root check failed: su 未返回 uid=0（{str(out)[:60]}）')
            return ok
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
        """
        把 XML 写回远端，并恢复属主/权限。

        为什么要显式回显一个成功标记：`adb_shell` 只返回 stdout，`cp`/`chmod`
        失败时 stderr 不会出现在返回值里，调用方拿到一个空串完全无法区分
        「写成功但没输出」和「根本没写进去」。这里在关键链末尾 echo 一个标记，
        标记缺席即判定写入失败。

        `restorecon`（SELinux 上下文）与临时文件清理用 `;` 串在标记之后：
        restorecon 在部分设备上不存在，不该因为它把「已经写进去了」判成失败。
        真正的最终判据仍然是紧随其后的回读校验 verify_applied。

        Returns:
            bool: 关键写入链（cp/chown/chmod）是否成功。
        """
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
            essential = [f'cp {TMP_REMOTE} {self.remote_path}']
            if uid:
                essential.append(f'chown {uid}:{gid} {self.remote_path}')
            essential.append(f'chmod {mode} {self.remote_path}')
            # 关键链成功才 echo 标记；后面的 restorecon / rm 失败不影响判定
            cmd = (f'{" && ".join(essential)} && echo {WRITE_OK_MARK} ; '
                   f'restorecon {self.remote_path} ; rm -f {TMP_REMOTE}')
            out = str(self._su(cmd))
            if WRITE_OK_MARK not in out:
                logger.error(f'ModPrefs: 写入 {self.remote_path} 失败'
                             f'（uid={uid} mode={mode}）：{out.strip()!r}')
                return False
            logger.info(f'ModPrefs: wrote {self.remote_path} (uid={uid} mode={mode})')
            return True
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

    def set_multiplier(self, mode: bool):
        """
        按策略把悬浮窗的倍率类开关写到目标值。

        流程：停游戏（若在跑）-> 写 XML -> 游戏保持关闭（ALAS 需要时会自己拉起）
        -> 回读校验。

        Args:
            mode: True = 启用倍率（恢复正常），False = 关闭倍率
        Returns:
            bool: 已是目标状态（视为已达成）或回读校验确认生效时为 True；
                  写入失败 / 校验未通过时为 False（调用方据此走「关失败」判定）；
                  无法安全操作（无 root、读不到文件、HandleError 关闭）时抛异常。
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
            # 幂等 = 已达成目标，返回 True。三个后端语义一致：
            # False 只留给「尝试过但没达成」，调用方据此区分正常跳过与真失败。
            return True

        logger.info(f'ModPrefs: applying {todo} -> {"ON" if mode else "OFF"}')

        # 必须停游戏：应用内存里的副本会在退出时把我们的写入覆盖回去。
        # 写完不主动拉起：ALAS 下一次截图发现游戏没跑（GameNotRunningError），
        # 会自动排一个 Restart 任务把它拉起来，顺带完成登录等待。
        self._stop_game()

        if not self.write_raw(self.build_xml(current, todo)):
            # 写不进去就直接失败：下面的回读校验虽然也能发现，但这里能给出
            # 更明确的「写入命令链失败」而不是含糊的「回读不一致」。
            logger.error('ModPrefs: 写入 prefs 失败，按失败处理')
            return False

        # 改配置文件这件事本身不会报错，所以必须回读验证；
        # 回读不一致 / 读不到明确状态都按失败处理，调用方会如实上报，
        # 不会带着"以为关了其实没关"的状态继续跑（封号风险的来源）。
        if not self.verify_applied(mode):
            logger.error('ModPrefs: 回读校验未确认生效，按失败处理')
            return False
        return True

    def repair(self, mode: bool):
        """
        只补写缺失的键（不改动已经正确的键）。

        与 set_multiplier 的区别：后者写的是完整的 OnKeys/OffKeys；
        这里只写"差一点"的那几个，代价更小，但同样需要停游戏才能落盘。

        Returns:
            bool: 回读校验确认补写生效时为 True；无需补写或校验未通过时为 False。
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

        logger.info(f'ModPrefs: repairing {todo} -> {"ON" if mode else "OFF"}')

        self._stop_game()

        if not self.write_raw(self.build_xml(current, todo)):
            logger.error('ModPrefs: 补写 prefs 失败，按失败处理')
            return False

        if not self.verify_applied(mode):
            logger.error('ModPrefs: 回读校验未确认生效，按失败处理')
            return False
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

    def _stop_game(self):
        """
        停游戏（写 prefs 的必要前置：应用退出时会把内存副本覆盖回文件）。

        ★ 不用 adb 强停来绕过 Alas.Error.HandleError=False。
        该配置的语义是「不要动游戏的启停」，此时 Device.app_stop 与 app_start
        都会抛 RequestHumanTakeover。如果这里改用 `am force-stop` 强停，
        游戏是停了，但下一步没人能把它拉起来：ALAS 截图收到 GameNotRunningError
        -> 排 Restart 任务 -> app_start 又抛 RequestHumanTakeover -> 实例 exit(1)。
        结果是「游戏被停在后台 + 实例崩掉」，比不做还糟。

        所以这种情况直接拒绝，把选择权交回用户：
        prefs 后端与 Error.HandleError=False 本质上互斥，要么换 overlay 后端
        （默认，零重启），要么打开 HandleError。

        ★ 但「游戏本来就没在跑」是这套互斥的例外：没有 app_stop 可调用，
        也就没有 app_start 需要人拉起来，代价为零。所以这个判定必须排在
        HandleError 之前 —— 否则实例刚启动（模拟器开着、游戏还没起来）时，
        HandleError=False 的用户会在这里抛异常，第一个任务又把实例停掉。
        """
        if not self._is_game_running():
            # 没在跑就不用停，也就不需要 HandleError 的许可。
            return
        if not getattr(self.config, 'Error_HandleError', True):
            raise RuntimeError(
                'ModPrefs 需要先停游戏才能安全写入 prefs，但 Alas.Error.HandleError '
                '已关闭 —— 该配置下 Device.app_stop / app_start 都会抛 '
                'RequestHumanTakeover，强停游戏后没人能把它拉起来，任务链会在下一步崩掉。'
                '请二选一：ModHandler.Backend 改为 overlay（默认，零重启），'
                '或打开 Alas.Error.HandleError。')
        logger.info('ModPrefs: stopping game, otherwise the running instance '
                    'overwrites our write when it exits')
        self.device.app_stop()

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
