"""
mod_discover — 发现改版客户端悬浮窗开关对应的 SharedPreferences key。

为什么需要它：
  JMBQ 改版客户端的功能开关以数字 ID 作为 SharedPreferences 的 key
  （如 <boolean name="22" value="true"/>、<int name="3" value="1000"/>），
  但「功能名 -> 数字 ID」的映射表只存在于打包后的客户端资源里，
  无法从 AL_Mod_Maker 离线推出，因此用「快照 -> 手动切换 -> 再快照 -> 求差集」来自动确定。

用法（在项目根目录，需要有可用的 adb 与已 root 的设备）：

  1) 采集基线（此时悬浮窗里的目标功能请保持「开」）：
       python dev_tools/mod_discover.py --serial 127.0.0.1:16416 snapshot baseline

  2) 在设备上打开悬浮窗，把「倍攻倍防」等要控制的功能手动关掉

  3) 采集对照并打印差异：
       python dev_tools/mod_discover.py --serial 127.0.0.1:16416 diff baseline

  输出会直接给出可粘贴进 ALAS 配置的 OffKeys / OnKeys 形式。

其他子命令：
  show       打印当前 prefs 全文
  check      环境自检（root / 包名 / prefs 文件 / 悬浮窗 / 模组特征）
"""
import argparse
import json
import os
import subprocess
import sys

DEFAULT_PACKAGE = 'com.bilibili.azurlane'
DEFAULT_PREFS_FILE = 'com.bilibili.azurlane_preferences'
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.mod_snapshots')


def find_adb():
    """优先用项目自带的 adb，其次用 PATH 里的。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(here, 'toolkit', 'Lib', 'site-packages', 'adbutils', 'binaries',
                     'adb.exe' if os.name == 'nt' else 'adb'),
        os.path.join(here, 'bin', 'adb.exe' if os.name == 'nt' else 'adb'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return 'adb'


class Adb:
    def __init__(self, serial):
        self.adb = find_adb()
        self.serial = serial
        self._ensure_connected()

    def _online_devices(self):
        try:
            r = subprocess.run([self.adb, 'devices'], capture_output=True, timeout=20)
        except Exception:
            return []
        out = []
        for line in r.stdout.decode('utf-8', 'replace').splitlines()[1:]:
            parts = line.split()
            # 只认状态为 device 的（unauthorized / offline 不算）
            if len(parts) >= 2 and parts[1] == 'device':
                out.append(parts[0])
        return out

    def _ensure_connected(self):
        """
        设备没在线就自动 adb connect 一次。

        模拟器掉线很常见（重启模拟器、adb server 被别的工具重启等），
        不自动重连的话表现是「读取失败: 」（后面什么都没有），很难懂。
        """
        if not self.serial or self.serial in self._online_devices():
            return
        try:
            r = subprocess.run([self.adb, 'connect', self.serial],
                               capture_output=True, timeout=30)
            msg = r.stdout.decode('utf-8', 'replace').strip()
            if msg:
                print(f'[i] {msg}')
        except Exception as e:
            print(f'[i] adb connect {self.serial} 失败: {e}')

    def raw(self, *args, timeout=30):
        cmd = [self.adb]
        if self.serial:
            cmd += ['-s', self.serial]
        cmd += list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            out = r.stdout.decode('utf-8', 'replace')
            err = r.stderr.decode('utf-8', 'replace').strip()
            if err and not out:
                # 把 adb 的报错带出来，否则只会看到「读取失败: 」
                return f'__ERROR__ {err}'
            return out
        except Exception as e:
            return f'__ERROR__ {e}'

    def su(self, cmd, timeout=30):
        # 关键：整条远端命令必须作为「一个」参数交给 su -c，
        # 否则 su 会把命令里的 -x 之类当成自己的选项（su: invalid option -- f）。
        quoted = "'" + str(cmd).replace("'", "'\\''") + "'"
        return self.raw('shell', f'su -c {quoted}', timeout=timeout)

    def shell(self, cmd, timeout=30):
        return self.raw('shell', str(cmd), timeout=timeout)


def prefs_path(package, prefs_file):
    return f'/data/data/{package}/shared_prefs/{prefs_file}.xml'


def parse_prefs(xml_text):
    """极简解析，避免依赖外部库。返回 {name: (type, value)}"""
    import re
    data = {}
    for m in re.finditer(r'<(boolean|int|float|long|string|set)\s+name="([^"]+)"\s+value="([^"]*)"',
                         xml_text):
        data[m.group(2)] = (m.group(1), m.group(3))
    return data


def read_prefs(adb, package, prefs_file):
    path = prefs_path(package, prefs_file)
    out = adb.su(f'cat {path}')
    if '__ERROR__' in out:
        return None, out
    if '<map' not in out:
        # 空输出 / 只有报错时，把最可能的原因一并说明，别只留一个冒号
        hint = (f'读取 {path} 返回了非 prefs 内容: {out.strip()[:200]!r}\n'
                f'    可能原因：\n'
                f'      1) 设备掉线 —— 先 `adb connect {adb.serial}`（本工具会尝试自动重连）\n'
                f'      2) 该包名没装改版客户端，或包名 / PrefsFile 不对\n'
                f'      3) su 不可用（设备没 root）')
        return None, hint
    return out, None


def cmd_show(args):
    adb = Adb(args.serial)
    xml, err = read_prefs(adb, args.package, args.prefs_file)
    if xml is None:
        print(f'[!] 读取失败: {err}')
        return 1
    print(xml)
    return 0


def cmd_check(args):
    adb = Adb(args.serial)
    print('===== mod_discover 环境自检 =====')
    print(f'adb              : {adb.adb}')
    print(f'serial           : {args.serial}')

    devices = adb.raw('devices')
    print(f'devices          : {devices.strip()!r}')

    ident = adb.su('id').strip()
    print(f'root (su -c id)  : {ident}')
    rooted = 'uid=0' in ident
    print(f'root available   : {rooted}')

    print(f'package          : {args.package}')
    pkgpath = adb.shell(f'pm path {args.package}').strip()
    print(f'pm path          : {pkgpath}')

    p = prefs_path(args.package, args.prefs_file)
    exists = adb.su(f'test -f {p} && echo YES || echo NO').strip()
    print(f'prefs file       : {p} -> {exists}')

    if rooted and exists == 'YES':
        xml, _ = read_prefs(adb, args.package, args.prefs_file)
        if xml:
            data = parse_prefs(xml)
            print(f'prefs entries    : {len(data)}')
            print('  数值型（很可能是倍率）:')
            for k, (t, v) in sorted(data.items(), key=lambda kv: kv[0]):
                if t in ('int', 'float', 'long'):
                    print(f'    {k:>6} = {v}  ({t})')
            print('  布尔型（很可能是功能开关）:')
            for k, (t, v) in sorted(data.items(), key=lambda kv: kv[0]):
                if t == 'boolean':
                    print(f'    {k:>6} = {v}')

    # 模组特征
    apk = pkgpath.replace('package:', '').strip()
    if apk:
        listing = adb.su(f'unzip -l "{apk}" 2>/dev/null | grep -E "libJMBQ|assets/dex|assets/bb|assets/key"')
        print(f'mod artifacts    : {listing.strip() or "(none -> 当前是原版客户端！)"}')

    # 悬浮窗：只看属于本包、且不是主 Activity 的窗口（避免把 systemui 的 ShellDropTarget 误判）
    windows = adb.shell('dumpsys window windows')
    pkg_windows = []
    for line in windows.splitlines():
        line = line.strip()
        if line.startswith('Window #') and args.package in line:
            pkg_windows.append(line)
    print(f'windows of pkg   : {len(pkg_windows)}')
    for w in pkg_windows:
        print(f'    {w}')
    overlay = [w for w in pkg_windows if 'MainActivity' not in w]
    print(f'overlay window   : {"FOUND -> " + overlay[0] if overlay else "not found"}')
    return 0


def _snap_file(name):
    os.makedirs(SNAP_DIR, exist_ok=True)
    return os.path.join(SNAP_DIR, f'{name}.json')


def cmd_snapshot(args):
    adb = Adb(args.serial)
    xml, err = read_prefs(adb, args.package, args.prefs_file)
    if xml is None:
        print(f'[!] 读取失败: {err}')
        return 1
    data = parse_prefs(xml)
    path = _snap_file(args.name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'[+] 已保存快照 {args.name}: {len(data)} 项 -> {path}')
    print('    接下来请在设备上打开悬浮窗，手动切换目标功能，然后运行 diff。')
    return 0


def cmd_diff(args):
    path = _snap_file(args.name)
    if not os.path.exists(path):
        print(f'[!] 快照不存在: {path}，请先运行 snapshot {args.name}')
        return 1
    with open(path, 'r', encoding='utf-8') as f:
        old = json.load(f)

    adb = Adb(args.serial)
    xml, err = read_prefs(adb, args.package, args.prefs_file)
    if xml is None:
        print(f'[!] 读取失败: {err}')
        return 1
    new = parse_prefs(xml)

    changed, added, removed = {}, {}, {}
    for k, v in new.items():
        if k not in old:
            added[k] = v
        elif old[k][1] != v[1]:
            changed[k] = (old[k], v)
    for k in old:
        if k not in new:
            removed[k] = old[k]

    print('===== 差异 =====')
    if not changed and not added and not removed:
        print('[!] 没有检测到任何变化。可能原因：')
        print('    - 切换后应用还没把新值落盘（请退出/切后台一次再试）')
        print('    - 该功能的开关没有存在这个 prefs 文件里')
        print('    - 悬浮窗改动只作用于内存，不持久化')
        return 1

    def fmt(v):
        t, val = v
        return f'{val}  [{t}]'

    for k, (o, n) in sorted(changed.items()):
        print(f'  CHANGED  {k:>8} : {fmt(o):<24} -> {fmt(n)}')
    for k, v in sorted(added.items()):
        print(f'  ADDED    {k:>8} : {fmt(v)}')
    for k, v in sorted(removed.items()):
        print(f'  REMOVED  {k:>8} : {fmt(v)}')

    # 推断：布尔变化 => 功能开关；数值变化 => 倍率数值
    off_keys, on_keys = [], []
    for k, (o, n) in changed.items():
        # o 是切换前（功能开），n 是切换后（功能关）
        on_keys.append(f'{k}={o[1]}')
        off_keys.append(f'{k}={n[1]}')

    if off_keys:
        print('\n===== 可粘贴进 ALAS 配置 =====')
        print(f'ModHandler.OffKeys = {",".join(off_keys)}')
        print(f'ModHandler.OnKeys  = {",".join(on_keys)}')
        print('\n（OffKeys = 关闭倍率时写入，OnKeys = 恢复时写回）')
    return 0


def _write_prefs(adb, package, prefs_file, new_xml):
    """把改好的 XML 写回设备，并恢复属主/权限。"""
    import tempfile
    remote = prefs_path(package, prefs_file)
    tmp = '/data/local/tmp/_alas_mod_discover.xml'
    stat = adb.su(f'stat -c "%u %g %a" {remote}').strip()
    m = re.match(r'(\d+)\s+(\d+)\s+(\d+)', stat)
    uid, gid, mode = (m.group(1), m.group(2), m.group(3)) if m else ('', '', '660')
    fd, local = tempfile.mkstemp(suffix='.xml', prefix='alas_discover_')
    os.close(fd)
    try:
        with open(local, 'w', encoding='utf-8') as f:
            f.write(new_xml)
        out = adb.raw('push', local, tmp)
        cmds = [f'cp {tmp} {remote}']
        if uid:
            cmds.append(f'chown {uid}:{gid} {remote}')
        cmds.append(f'chmod {mode} {remote}')
        cmds.append(f'restorecon {remote}')
        adb.su(' && '.join(cmds) + f' ; rm -f {tmp}')
        return True
    finally:
        try:
            os.remove(local)
        except OSError:
            pass


def cmd_set(args):
    """
    把某个 key 直接改成指定值，用来验证「这个编号到底控制哪个功能」。

    流程：
      1) 先 snapshot 备份（例如 snapshot probe）
      2) set 35 false        ← 游戏重启后看「以德服人」是否真的关了
      3) restore probe       ← 还原

    注意：游戏运行中改文件可能无效、且会被内存副本覆盖，所以要先停游戏。
    """
    adb = Adb(args.serial)
    xml, err = read_prefs(adb, args.package, args.prefs_file)
    if xml is None:
        print(f'[!] 读取失败: {err}')
        return 1

    from module.mod_handler.mod_prefs import ModPrefs
    parsed = ModPrefs.parse(xml)
    if str(args.key) not in parsed:
        print(f'[!] prefs 里没有 key={args.key}。现有 key: {sorted(parsed)}')
        return 1
    old = parsed[str(args.key)]
    value = args.value
    if value.lower() in ('true', 'false'):
        value = value.lower() == 'true'
    else:
        try:
            value = int(value)
        except ValueError:
            pass

    print(f'[i] key={args.key}: {old[1]!r} ({old[0]}) -> {value!r}')
    pid = adb.shell(f'pidof {args.package}').strip()
    if pid:
        print('[i] 检测到游戏在运行，先停掉（否则写入会被内存副本覆盖）')
        adb.shell(f'am force-stop {args.package}')
        time.sleep(2)

    new_xml = ModPrefs.build_xml(xml, {str(args.key): value})
    if not _write_prefs(adb, args.package, args.prefs_file, new_xml):
        print('[!] 写入失败')
        return 1

    check, _ = read_prefs(adb, args.package, args.prefs_file)
    now = ModPrefs.parse(check).get(str(args.key)) if check else None
    print(f'[+] 已写入，回读 key={args.key} = {now}')
    print('\n下一步：启动游戏，打开悬浮窗看那个功能有没有真的变化。')
    print(f'  - 变了   -> 这个 key({args.key}) 就是它，记下来填进 OffKeys/OnKeys')
    print(f'  - 没变   -> 这个 key 不是它，换一个再试（或 restore 还原）')
    print(f'  还原：toolkit\\python.exe {os.path.basename(__file__)} '
          f'--serial {args.serial} restore <快照名>')
    return 0


def cmd_restore(args):
    """用之前的快照还原 prefs（set 之后收尾用）。"""
    adb = Adb(args.serial)
    path = _snap_file(args.name)
    if not os.path.exists(path):
        print(f'[!] 快照不存在: {path}')
        return 1
    with open(path, 'r', encoding='utf-8') as f:
        snap = json.load(f)

    xml, err = read_prefs(adb, args.package, args.prefs_file)
    if xml is None:
        print(f'[!] 读取失败: {err}')
        return 1

    from module.mod_handler.mod_prefs import ModPrefs
    parsed = ModPrefs.parse(xml)
    restore = {}
    for k, (t, v) in snap.items():
        if str(k) not in parsed or str(parsed[str(k)][1]) != str(v):
            if t == 'boolean':
                restore[str(k)] = v == 'true'
            elif t in ('int', 'long'):
                restore[str(k)] = int(v)
            elif t == 'float':
                restore[str(k)] = float(v)
            else:
                continue
    if not restore:
        print('[i] 已经和快照一致，无需还原')
        return 0

    pid = adb.shell(f'pidof {args.package}').strip()
    if pid:
        print('[i] 先停游戏')
        adb.shell(f'am force-stop {args.package}')
        time.sleep(2)
    new_xml = ModPrefs.build_xml(xml, restore)
    if not _write_prefs(adb, args.package, args.prefs_file, new_xml):
        print('[!] 还原失败')
        return 1
    print(f'[+] 已按快照 {args.name} 还原 {len(restore)} 项: {restore}')
    return 0


def main():
    # Windows 控制台默认不是 UTF-8，中文会乱码
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

    ap = argparse.ArgumentParser(description='发现改版客户端悬浮窗开关的 prefs key')
    ap.add_argument('--serial', default='127.0.0.1:16416', help='adb serial')
    ap.add_argument('--package', default=DEFAULT_PACKAGE)
    ap.add_argument('--prefs-file', default=DEFAULT_PREFS_FILE)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('show', help='打印当前 prefs').set_defaults(func=cmd_show)
    sub.add_parser('check', help='环境自检').set_defaults(func=cmd_check)

    s1 = sub.add_parser('snapshot', help='采集基线快照')
    s1.add_argument('name')
    s1.set_defaults(func=cmd_snapshot)

    s2 = sub.add_parser('diff', help='与快照对比并给出 key 映射')
    s2.add_argument('name')
    s2.set_defaults(func=cmd_diff)

    s3 = sub.add_parser('set', help='直接改某个 key（用来验证编号对应哪个功能）')
    s3.add_argument('key', help='prefs 里的编号，例如 35')
    s3.add_argument('value', help='目标值，例如 false / true / 1 / 1000')
    s3.set_defaults(func=cmd_set)

    s4 = sub.add_parser('restore', help='用快照还原 prefs（set 之后收尾）')
    s4.add_argument('name')
    s4.set_defaults(func=cmd_restore)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
