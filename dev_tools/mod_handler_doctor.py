"""
mod_handler_doctor — 真机自检：确认「悬浮窗倍率控制」在当前环境下真的能用。

它只读不写：
  * 检查 adb / root / 包名 / prefs 文件
  * 打印 prefs 里所有数值型（很可能是倍率）与布尔型（很可能是功能开关）条目
  * 校验 ModHandler.OffKeys / OnKeys 指向的 key 是否真实存在、当前值是什么
  * 给出「现在该开还是该关」的判定结果

不会写 prefs、不会开关倍率、不会重启游戏。
真正改倍率只发生在 ALAS 调度任务时（alas.py 的 ModHandler 钩子）。

用法（项目根目录）：
    toolkit\\python.exe dev_tools/mod_handler_doctor.py
    toolkit\\python.exe dev_tools/mod_handler_doctor.py --config "alas官服jmbq"
    toolkit\\python.exe dev_tools/mod_handler_doctor.py --serial 127.0.0.1:16416
    toolkit\\python.exe dev_tools/mod_handler_doctor.py --offline   # 不连设备，只看配置

key 映射（OffKeys / OnKeys）的发现流程见 dev_tools/mod_discover.py。
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DEFAULT_PACKAGE = 'com.bilibili.azurlane'
DEFAULT_PREFS = 'com.bilibili.azurlane_preferences'


def find_adb():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for c in [os.path.join(here, 'toolkit', 'Lib', 'site-packages', 'adbutils',
                           'binaries', 'adb.exe' if os.name == 'nt' else 'adb'),
              os.path.join(here, 'bin', 'adb.exe' if os.name == 'nt' else 'adb')]:
        if os.path.exists(c):
            return c
    return 'adb'


class Adb:
    def __init__(self, serial):
        self.adb = find_adb()
        self.serial = serial

    def raw(self, *args, timeout=30):
        cmd = [self.adb]
        if self.serial:
            cmd += ['-s', self.serial]
        cmd += list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            return r.stdout.decode('utf-8', 'replace')
        except Exception as e:
            return f'__ERROR__ {e}'

    def shell(self, cmd, timeout=30):
        return self.raw('shell', str(cmd), timeout=timeout)

    def su(self, cmd, timeout=30):
        # 整条远端命令必须作为「一个」参数交给 su -c，否则 su 会把命令里的 -x 当成自己的选项
        quoted = "'" + str(cmd).replace("'", "'\\''") + "'"
        return self.raw('shell', f'su -c {quoted}', timeout=timeout)


def parse_prefs(xml_text):
    from module.mod_handler.mod_prefs import ModPrefs
    return ModPrefs.parse(xml_text)


def pick_serial(adb, prefer=None):
    if prefer:
        return prefer
    out = adb.raw('devices')
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            return parts[0]
    return None


def main():
    ap = argparse.ArgumentParser(description='悬浮窗倍率控制真机自检（只读）')
    ap.add_argument('--serial', default=None, help='adb serial，缺省取第一个在线设备')
    ap.add_argument('--config', default=None, help='ALAS 配置名，缺省取 config/*.json 里的第一个实例')
    ap.add_argument('--package', default=None, help='游戏包名，缺省读 ALAS 配置')
    ap.add_argument('--prefs-file', default=None, help='prefs 文件名（不含 .xml），缺省读 ALAS 配置')
    ap.add_argument('--offline', action='store_true', help='只看 ALAS 配置，不连设备')
    args = ap.parse_args()

    print('=' * 78)
    print('  mod_handler_doctor — 悬浮窗倍率控制真机自检（只读，不会改动任何设置）')
    print('=' * 78)

    # ---------------------------------------------------------- 1. ALAS 配置
    print('\n[1] ALAS 配置')
    config = None
    config_serial = None
    try:
        from module.config.config import AzurLaneConfig
        from module.config.deep import deep_get
        from module.config.utils import alas_instance
        name = args.config
        if name is None:
            instances = alas_instance()
            name = instances[0] if instances else 'alas'
        print(f'  配置实例      : {name}')
        config = AzurLaneConfig(config_name=name)
        config_serial = deep_get(config.data, 'Alas.Emulator.Serial', default=None)
        from module.mod_handler.mod_handler import ModHandler
        # device='skip' -> 不构造 Device，避免多余的模拟器探测日志
        handler = ModHandler(config=config, device='skip')
        print(f'  配置里的设备  : {config_serial}')
        print(f'  ModHandler.Enabled        : {handler.enabled}')
        print(f'  ModHandler.Backend        : {handler.backend}')
        print(f'  ModHandler.SensitiveTask  : {handler.sensitive_option}')
        print(f'  敏感任务（倍率关闭）      : {handler.diagnose()["off_tasks"]}')
        print(f'  OffKeys                   : {handler.off_keys}')
        print(f'  OnKeys                    : {handler.on_keys}')
        print(f'  本地状态缓存              : {handler.get_state()} (decided={handler.get_decided()})')
        if not handler.enabled:
            print('  [!] ModHandler.Enabled = False：ALAS 不会主动开关倍率')
            print('      （仅当敏感任务里倍率仍开着时才会强制关闭）')
        if not handler.keys_configured:
            print('  [!] OffKeys / OnKeys 都是空的：无法开关倍率')
            print('      请先跑 dev_tools/mod_discover.py 发现 key 映射')
    except Exception as e:
        print(f'  [!] 读取 ALAS 配置失败: {type(e).__name__}: {e}')
        print('      可以只用 --package/--prefs-file 做设备侧检查')

    package = args.package or DEFAULT_PACKAGE
    prefs_file = args.prefs_file or DEFAULT_PREFS
    if config is not None:
        from module.config.deep import deep_get
        package = args.package or deep_get(config.data, 'ModHandler.PackageName',
                                           default=DEFAULT_PACKAGE) or DEFAULT_PACKAGE
        prefs_file = args.prefs_file or deep_get(config.data, 'ModHandler.PrefsFile',
                                                 default=DEFAULT_PREFS) or DEFAULT_PREFS

    if args.offline:
        print('\n[2] 已指定 --offline，跳过设备检查')
        print(f'  package       : {package}')
        print(f'  prefs 文件    : {prefs_file}')
        print('\n  下一步：去掉 --offline 跑真机检查，或直接跑')
        print('    toolkit\\python.exe dev_tools/mod_handler_doctor.py')
        return 0

    # ---------------------------------------------------------- 2. 设备
    print('\n[2] 设备与 root')
    adb = Adb(None)
    if args.serial:
        serial = args.serial
    elif config_serial and config_serial != 'auto':
        # 与 ALAS 保持一致：用配置里指定的设备，而不是 adb devices 的第一个
        serial = config_serial
    else:
        serial = pick_serial(adb, None)
    if serial is None:
        print('  [!] 没有在线设备，请先启动模拟器并 adb connect')
        return 1
    adb = Adb(serial)
    print(f'  adb           : {adb.adb}')
    print(f'  serial        : {serial}' + ('  (来自 ALAS 配置 Alas.Emulator.Serial)'
                                           if not args.serial and serial == config_serial else ''))
    ident = adb.su('id').strip()
    rooted = 'uid=0' in ident
    print(f'  su -c id      : {ident or "(空)"}')
    print(f'  root 可用     : {rooted}')
    if not rooted:
        print('  [!] 没有 root：prefs 后端不可用，请把 ModHandler.Backend 设为 ui')
        print('      或者用 --serial 指定已 root 的那台（例如 MuMu 的 127.0.0.1:16416）')

    print(f'\n[3] 游戏包与 prefs 文件')
    pkgpath = adb.shell(f'pm path {package}').strip()
    print(f'  package       : {package}')
    print(f'  pm path       : {pkgpath or "(未安装)"}')
    if not pkgpath:
        print('  [!] 该包名未安装，请检查 ModHandler.PackageName')
        return 1

    remote = f'/data/data/{package}/shared_prefs/{prefs_file}.xml'
    exists = adb.su(f'test -f {remote} && echo YES || echo NO').strip()
    print(f'  prefs 文件    : {remote} -> {exists}')
    if exists != 'YES':
        print('  [!] prefs 文件不存在：')
        print('      - 确认 ModHandler.PrefsFile 是否正确（默认 com.bilibili.azurlane_preferences）')
        print('      - 确认客户端是改版客户端（JMBQ / azurlan），原版客户端没有悬浮窗')
        return 1

    # ---------------------------------------------------------- 4. 内容
    print('\n[4] prefs 内容')
    xml = adb.su(f'cat {remote}')
    if '<map' not in xml:
        print(f'  [!] 读取失败: {xml[:200]}')
        return 1
    data = parse_prefs(xml)
    print(f'  条目数        : {len(data)}')

    ints = sorted([(k, v) for k, v in data.items() if v[0] in ('int', 'float', 'long')],
                  key=lambda kv: int(kv[0]) if kv[0].lstrip('-').isdigit() else 9999)
    bools = sorted([(k, v) for k, v in data.items() if v[0] == 'boolean'],
                   key=lambda kv: int(kv[0]) if kv[0].lstrip('-').isdigit() else 9999)
    print('\n  数值型（倍率类最可能是这些，值是 1 通常代表「关闭/无加成」）:')
    for k, (t, v) in ints:
        print(f'    {k:>6} = {v:<8} ({t})')
    print('\n  布尔型（功能开关）:')
    for k, (t, v) in bools:
        print(f'    {k:>6} = {v}')

    # ---------------------------------------------------------- 5. key 校验
    print('\n[5] OffKeys / OnKeys 校验')
    if config is None:
        print('  (跳过：没有读到 ALAS 配置)')
        return 0
    from module.mod_handler.mod_handler import parse_key_values
    from module.config.deep import deep_get
    off = parse_key_values(deep_get(config.data, 'ModHandler.OffKeys', default=''))
    on = parse_key_values(deep_get(config.data, 'ModHandler.OnKeys', default=''))
    if not off and not on:
        print('  OffKeys / OnKeys 均为空 —— 功能不会生效。')
        print('  下一步：')
        print('    1) 保持悬浮窗里倍率「开」，跑 snapshot：')
        print(f'       toolkit\\python.exe dev_tools/mod_discover.py --serial {serial} snapshot baseline')
        print('    2) 在游戏悬浮窗里手动把倍率关掉（退出/切后台让它落盘）')
        print('    3) 跑 diff，把输出的 OffKeys / OnKeys 填进 ALAS 配置：')
        print(f'       toolkit\\python.exe dev_tools/mod_discover.py --serial {serial} diff baseline')
        return 0

    ok = True
    for label, mapping in (('OffKeys', off), ('OnKeys', on)):
        if not mapping:
            print(f'  {label}: (空)')
            continue
        print(f'  {label}:')
        for k, want in mapping.items():
            cur = data.get(str(k))
            if cur is None:
                ok = False
                print(f'    {k:>6}: 期望 {want!r} —— [!] prefs 里没有这个 key，配置很可能写错了')
            else:
                flag = '一致' if str(cur[1]) == str(want).lower() or str(cur[1]) == str(want) else '不同'
                print(f'    {k:>6}: 期望 {want!r} / 实际 {cur[1]!r}  ({flag})')

    # ---------------------------------------------------------- 6. 判定
    print('\n[6] 当前倍率判定')
    from module.mod_handler.mod_handler import ModHandler
    handler = ModHandler(config=config, device='skip')

    def match(mapping):
        if not mapping:
            return False
        for k, want in mapping.items():
            cur = data.get(str(k))
            if cur is None or str(cur[1]) != str(want).lower():
                return False
        return True

    # 直接用刚读到的 XML 判定，避免再起一次 adb
    if match(off):
        state = False
    elif match(on):
        state = True
    else:
        state = None

    print(f'  设备上的倍率  : {"ON（开启）" if state else "OFF（关闭）" if state is False else "无法判定"}')
    print(f'  本地状态缓存  : {handler.get_state()}')
    if state is not None and handler.get_state() is not None and state != handler.get_state():
        print('  [i] 设备状态与本地缓存不一致：下次任务开始时会自动按策略纠正')

    # ---------------------------------------------------------- 7. 悬浮窗
    print('\n[7] 悬浮窗')
    windows = adb.shell('dumpsys window windows')
    pkg_windows = [l.strip() for l in windows.splitlines()
                   if l.strip().startswith('Window #') and package in l]
    print(f'  属于本包的窗口: {len(pkg_windows)}')
    for w in pkg_windows:
        print(f'    {w}')
    overlay = [w for w in pkg_windows if 'MainActivity' not in w]
    if overlay:
        print(f'  悬浮窗: FOUND -> {overlay[0]}')
    else:
        print('  悬浮窗: 未发现（游戏没启动时是正常的）')

    print('\n[8] 结论')
    print(f'  root          : {"OK" if rooted else "NO"}')
    print(f'  prefs 可读    : OK')
    print(f'  key 映射      : {"OK" if ok else "有问题，见第 5 节"}')
    print(f'  倍率状态      : {"ON" if state else "OFF" if state is False else "UNKNOWN"}')
    print('\n  说明：本工具只读。真正的开关动作由 ALAS 调度任务时执行：')
    print('        敏感任务（演习 / META / 共斗）自动关闭，其他任务自动启用。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
