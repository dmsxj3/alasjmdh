"""
ModPrefs 自检 —— XML 读写逻辑 + set_multiplier 的完整编排（假设备，不碰真机）。

覆盖：
  1. parse / build_xml 往返、类型变化、新增 key、其它条目与顺序保留
  2. 值格式化
  3. set_multiplier 的完整编排：读 XML -> 停游戏 -> 推送并覆盖 -> 游戏保持关闭
     （写完不主动拉起，ALAS 发现游戏没跑会自动排 Restart 任务）
  4. 已是目标状态时不写、不停
  5. root 不可用 / 读不到 prefs / OffKeys 为空 时的行为
  6. get_state 的三态判定与「用户手动改动」识别
  7. 写入后回读校验失败必须如实返回 False（不谎报成功）
  8. Alas.Error.HandleError 关闭时停游戏退化为 am force-stop

真实设备只读验证在 dev_tools/mod_handler_doctor.py，那里会用你的模拟器实测。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mod_handler_testkit import (  # noqa: E402
    Checker, FakeConfig, FakeDevice, RequestHumanTakeover, install_stubs,
)

install_stubs()

from module.mod_handler.mod_prefs import ModPrefs  # noqa: E402

checker = Checker('ModPrefs 自检')
check = checker.check
eq = checker.eq

parse = ModPrefs.parse
build_xml = ModPrefs.build_xml
format_value = ModPrefs._format_value

# 取自真机 /data/data/com.bilibili.azurlane/shared_prefs/com.bilibili.azurlane_preferences.xml
SAMPLE = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <boolean name="22" value="true" />
    <boolean name="23" value="true" />
    <boolean name="35" value="true" />
    <boolean name="25" value="true" />
    <boolean name="-1" value="true" />
    <boolean name="27" value="true" />
    <int name="-3" value="2" />
    <boolean name="28" value="true" />
    <int name="-4" value="16" />
    <boolean name="29" value="true" />
    <int name="-98" value="5" />
    <int name="1" value="1000" />
    <int name="2" value="1000" />
    <int name="3" value="1000" />
    <boolean name="30" value="true" />
    <boolean name="21" value="true" />
    <boolean name="32" value="true" />
</map>
"""

PREF_PATH = '/data/data/com.bilibili.azurlane/shared_prefs/com.bilibili.azurlane_preferences.xml'
TMP_REMOTE = '/data/local/tmp/_alas_mod_prefs.xml'

# ---------------------------------------------------------------- 1. XML
checker.header('1. XML 解析与改写')
data = parse(SAMPLE)
eq('解析出 17 项', len(data), 17)
eq('boolean 22', data.get('22'), ('boolean', 'true'))
eq('int 3', data.get('3'), ('int', '1000'))
eq('负数 key -98', data.get('-98'), ('int', '5'))

new_xml = build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1})
nd = parse(new_xml)
eq('关倍率: 1 -> 1', nd['1'], ('int', '1'))
eq('关倍率: 2 -> 1', nd['2'], ('int', '1'))
eq('关倍率: 3 -> 1', nd['3'], ('int', '1'))
check('其它布尔项未受影响',
      nd['22'] == ('boolean', 'true') and nd['21'] == ('boolean', 'true'))
check('其它数值项未受影响', nd['-98'] == ('int', '5') and nd['-3'] == ('int', '2'))
eq('条目数不变', len(nd), 17)
check('保留 xml 声明', new_xml.startswith(
    "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>"))

b = parse(build_xml(SAMPLE, {'22': False}))
eq('布尔开关 22 -> false', b['22'], ('boolean', 'false'))
eq('同类开关 23 保持 true', b['23'], ('boolean', 'true'))

t = parse(build_xml(SAMPLE, {'22': 5}))
eq('类型变化 boolean -> int', t['22'], ('int', '5'))

a = parse(build_xml(SAMPLE, {'99': True, '100': 7}))
eq('新增布尔 key', a.get('99'), ('boolean', 'true'))
eq('新增整数 key', a.get('100'), ('int', '7'))
eq('新增后原有条目仍在', (a['1'], a['22']), (('int', '1000'), ('boolean', 'true')))

eq('bool True 格式化', format_value(True), 'true')
eq('bool False 格式化', format_value(False), 'false')
eq('int 格式化', format_value(5), '5')
eq('float 格式化', format_value(1.5), '1.5')

# ---------------------------------------------------------------- 2. 假设备
checker.header('2. set_multiplier 编排（假设备）')


class ScriptedDevice(FakeDevice):
    """让 adb_shell 能真的「读」到 prefs，并记录推送的 XML 内容。"""

    def __init__(self, xml=SAMPLE, root=True, running=True, **kwargs):
        super().__init__(root=root, **kwargs)
        self.xml = xml
        self.running = running
        self.raise_on_app_stop = None
        self.raise_on_app_start = None

    def adb_shell(self, cmd, timeout=10, **kwargs):
        # 交给基类记录调用（列表形式会被拼成字符串），再按需返回内容
        cmd = super().adb_shell(cmd, timeout=timeout, **kwargs)
        if isinstance(cmd, str) and cmd.startswith('su -c'):
            inner = cmd[len('su -c'):].strip().strip("'").replace("'\\''", "'")
            return self._su(inner)
        return cmd

    # _su 直接用 testkit 里那套「段内 && 串联、段间 ; 分隔」的最小 shell 模拟：
    # write_raw 现在靠命令链末尾的 echo 标记判定成功，自己再实现一份很容易
    # 漏掉标记，把成功的写入误判成失败。

    def adb_push(self, local, remote):
        super().adb_push(local, remote)


def make_prefs(**config_values):
    cfg = FakeConfig(**config_values)
    dev = ScriptedDevice()
    return ModPrefs(config=cfg, device=dev), cfg, dev


# 2.1 关倍率：停游戏 -> 写入 -> 游戏保持关闭（ALAS 需要时会自己拉起）
p, cfg, dev = make_prefs()
changed = p.set_multiplier(False)
check('关倍率返回 True', changed is True)
eq('设备上 1/2/3 已被写成 1',
   (parse(dev.xml)['1'], parse(dev.xml)['2'], parse(dev.xml)['3']),
   (('int', '1'), ('int', '1'), ('int', '1')))
check('写入前停游戏', 'app_stop' in dev.calls, str(dev.calls))
check('写完不主动拉起游戏（交给 ALAS 的 Restart 任务）', 'app_start' not in dev.calls,
      str(dev.calls))
check('先停后写',
      dev.calls.index('app_stop')
      < dev.calls.index('adb_push:/data/local/tmp/_alas_mod_prefs.xml'), str(dev.calls))
check('推送后 cp 到真实路径',
      any('cp /data/local/tmp/_alas_mod_prefs.xml ' + PREF_PATH in c for c in dev.calls),
      str([c for c in dev.calls if c.startswith('adb_shell')]))
check('恢复属主与权限',
      any('chown 10046:10046' in c for c in dev.calls)
      and any('chmod 660' in c for c in dev.calls))
check('清理临时文件', any('rm -f /data/local/tmp/_alas_mod_prefs.xml' in c for c in dev.calls))

# 2.2 再开回来（2.1 已把倍率关掉、游戏停了；设备先处于「关」状态，直接写）
p, cfg, dev = make_prefs()
dev.xml = build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1})
dev.running = False
changed = p.set_multiplier(True)
check('开倍率返回 True', changed is True)
eq('设备上 1/2/3 已回到 1000',
   (parse(dev.xml)['1'], parse(dev.xml)['2'], parse(dev.xml)['3']),
   (('int', '1000'), ('int', '1000'), ('int', '1000')))
check('游戏没在跑就不停', 'app_stop' not in dev.calls, str(dev.calls))

# 2.3 幂等：设备已是目标状态时只读探测，不发生任何写动作
p, cfg, dev = make_prefs()
dev.xml = build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1})    # 设备上已经是「关」
dev.calls.clear()
changed = p.set_multiplier(False)
# 幂等返回 True（已达成）：与 overlay/ui 后端语义一致，
# False 只留给「尝试过但没达成」，策略层据此区分正常跳过与真失败
check('已是目标状态时返回 True（已达成）', changed is True)
writes = [c for c in dev.calls if c.startswith(('app_stop', 'app_start', 'adb_push'))]
eq('已是目标状态时不产生任何写动作', writes, [])

# 2.4 OffKeys 为空
p, cfg, dev = make_prefs(OffKeys='')
check('OffKeys 为空时返回 False', p.set_multiplier(False) is False)
eq('OffKeys 为空时不碰设备', dev.calls, [])

# 2.5 非 root
p, cfg, dev = make_prefs()
dev.root = False
try:
    p.set_multiplier(False)
    check('非 root 时抛异常', False, '没有抛异常')
except RuntimeError as e:
    check('非 root 时抛 RuntimeError', 'root' in str(e), str(e))
check('非 root 时不动游戏进程', 'app_stop' not in dev.calls, str(dev.calls))

# 2.6 读不到 prefs
p, cfg, dev = make_prefs()
dev.xml = None
try:
    p.set_multiplier(False)
    check('读不到 prefs 时抛异常', False, '没有抛异常')
except RuntimeError as e:
    check('读不到 prefs 时抛 RuntimeError', '无法读取' in str(e), str(e))

# 2.7 游戏没在跑就不需要重启
p, cfg, dev = make_prefs()
dev.running = False
dev.calls.clear()
p.set_multiplier(False)
check('游戏未运行时不停不启', 'app_stop' not in dev.calls and 'app_start' not in dev.calls,
      str(dev.calls))
check('游戏未运行时仍完成写入', parse(dev.xml)['1'] == ('int', '1'))

# 2.8 写入抛异常时向上传播；游戏保持关闭，交给 ALAS 的错误处理接管


def boom(self, xml):
    raise RuntimeError('push failed')


p, cfg, dev = make_prefs()
original = ModPrefs.write_raw
ModPrefs.write_raw = boom
try:
    p.set_multiplier(False)
    check('写入失败时向上抛异常', False, '没有抛异常')
except RuntimeError as e:
    check('写入失败时向上抛异常', 'push failed' in str(e))
finally:
    ModPrefs.write_raw = original
check('写入失败后游戏保持关闭', 'app_start' not in dev.calls, str(dev.calls))

# 2.9 HandleError 关闭时不再用 adb 强停绕过。
# 旧实现 catch RequestHumanTakeover 后改用 `am force-stop`：游戏是停了，
# 但 app_start 同样会抛 —— 下一步截图 GameNotRunningError -> 排 Restart
# -> app_start 抛异常 -> 实例 exit(1)，等于「游戏被停在后台 + 实例崩掉」。
# 现在直接拒绝并给出可操作的提示。
p, cfg, dev = make_prefs()
cfg.Error_HandleError = False
try:
    p.set_multiplier(False)
    check('HandleError 关闭时拒绝强停游戏', False, '没有抛异常')
except RuntimeError as e:
    check('HandleError 关闭时拒绝强停游戏', 'HandleError' in str(e), str(e))
check('HandleError 关闭时完全不碰游戏启停',
      'app_stop' not in dev.calls and 'app_start' not in dev.calls
      and not any('force-stop' in c for c in dev.calls), str(dev.calls))
eq('HandleError 关闭时也没动过 prefs', parse(dev.xml)['1'], ('int', '1000'))

# 2.9b 其它原因导致 app_stop 失败时如实向上抛，不掩盖
p, cfg, dev = make_prefs()
dev.raise_on_app_stop = RequestHumanTakeover('device busy')
try:
    p.set_multiplier(False)
    check('app_stop 其它异常时向上传播', False, '没有抛异常')
except RequestHumanTakeover:
    check('app_stop 其它异常时向上传播', True)
check('app_stop 失败后不启动游戏', 'app_start' not in dev.calls, str(dev.calls))

# 2.9c write_raw 的失败判定：命令链没回显成功标记就按失败处理
p, cfg, dev = make_prefs()
dev.fail_command = 'cp /data/local/tmp/_alas_mod_prefs.xml'
changed = p.set_multiplier(False)
eq('cp 失败时 set_multiplier 返回 False（不谎报成功）', changed, False)
eq('cp 失败时设备上的值没变', parse(dev.xml)['1'], ('int', '1000'))

p, cfg, dev = make_prefs()
eq('write_raw 命令链成功时返回 True',
   p.write_raw(p.build_xml(dev.xml, {'1': 1})), True)
eq('write_raw 成功后设备上的值已更新', parse(dev.xml)['1'], ('int', '1'))

# 2.10 写入后回读校验
p, cfg, dev = make_prefs()                       # 设备上倍率开着
eq('verify_applied 与设备实际相符时返回 True', p.verify_applied(True), True)
eq('verify_applied 与设备实际不符时返回 False', p.verify_applied(False), False)
p, cfg, dev = make_prefs()
dev.xml = build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1})   # 设备上倍率关着
eq('设备关着时 verify_applied(False) 返回 True', p.verify_applied(False), True)
p, cfg, dev = make_prefs()
dev.xml = build_xml(SAMPLE, {'1': 1})       # 半开半关 -> 读不到明确状态
eq('verify_applied 读不到状态时返回 None', p.verify_applied(False), None)

# 2.11 回读校验失败必须如实返回 False —— "以为关了其实没关"正是封号风险
p, cfg, dev = make_prefs()                       # 设备上倍率开着，目标是关
_unchanged_xml = dev.xml
_real_write = ModPrefs.write_raw
# 模拟"写入没有生效"：推送的内容仍是旧值，回读自然对不上
ModPrefs.write_raw = lambda self, xml: _real_write(self, _unchanged_xml)
try:
    changed = p.set_multiplier(False)
    eq('回读校验失败时返回 False（不谎报成功）', changed, False)
finally:
    ModPrefs.write_raw = _real_write

# ---------------------------------------------------------------- 3. get_state
checker.header('3. get_state 三态判定')


def state_case(xml, **config_values):
    p, cfg, dev = make_prefs(**config_values)
    dev.xml = xml
    return p.get_state()


eq('倍率开着 -> True', state_case(SAMPLE), True)
eq('倍率关着 -> False',
   state_case(build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1})), False)
eq('用户手动改了一半 -> None',
   state_case(build_xml(SAMPLE, {'1': 1})), None)
eq('OffKeys/OnKeys 都没配 -> None', state_case(SAMPLE, OffKeys='', OnKeys=''), None)

# ---------------------------------------------------------------- 3b. describe_state（GUI 状态栏）
checker.header('3b. describe_state 给 GUI 的状态描述')

def describe_case(xml, **config_values):
    p, cfg, dev = make_prefs(**config_values)
    dev.xml = xml
    return p.describe_state()


_eq = describe_case(SAMPLE)
eq('开着 -> option=on', _eq['option'], 'on')
check('明细里带当前值与目标值', '1=1000' in _eq['detail'] and '目标1000' in _eq['detail'],
      _eq['detail'])

_eq = describe_case(build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1}))
eq('关着 -> option=off', _eq['option'], 'off')

_eq = describe_case(build_xml(SAMPLE, {'1': 1}))
eq('半开半关 -> option=unknown', _eq['option'], 'unknown')
check('unknown 时明细提示不在目标状态', '不在' in _eq['detail'], _eq['detail'])

_eq = describe_case(SAMPLE, OffKeys='', OnKeys='')
eq('没配 key -> option=unconfigured', _eq['option'], 'unconfigured')
check('unconfigured 时提示去跑 mod_discover', 'mod_discover' in _eq['detail'], _eq['detail'])

p, cfg, dev = make_prefs()
dev.root = False
eq('非 root -> option=unknown', p.describe_state()['option'], 'unknown')

p, cfg, dev = make_prefs()
dev.xml = None
eq('读不到 prefs -> option=unknown', p.describe_state()['option'], 'unknown')

p, cfg, dev = make_prefs()
dev.root = False
eq('非 root -> None', p.get_state(), None)

p, cfg, dev = make_prefs()
dev.xml = None
eq('读不到 prefs -> None', p.get_state(), None)

# 布尔型开关同样支持
BOOL_SAMPLE = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <boolean name="22" value="true" />
    <boolean name="23" value="true" />
</map>
"""
eq('布尔开关：开着 -> True',
   state_case(BOOL_SAMPLE, OffKeys='22=false,23=false', OnKeys='22=true,23=true'), True)
eq('布尔开关：关着 -> False',
   state_case(BOOL_SAMPLE.replace('"true"', '"false"'),
              OffKeys='22=false,23=false', OnKeys='22=true,23=true'), False)

# ---------------------------------------------------------------- 3c. 部分匹配时只补写缺失的键
checker.header('3c. needs_repair / repair（用户手动拨过开关的中间状态）')

_REPAIR_OFF = '1=1,2=1,3=1,22=false'
_REPAIR_ON = '1=1000,2=1000,3=1000,22=true'
_ALREADY_OFF = build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1, '22': False})
_ALREADY_ON = build_xml(SAMPLE, {'1': 1000, '2': 1000, '3': 1000, '22': True})
# 中间态：倍率已经关了，但以德服人（22）还开着 —— 手动拨过开关就会出现
_MIXED = build_xml(SAMPLE, {'1': 1, '2': 1, '3': 1, '22': True})
# 陌生值：既不是目标值也不是另一个目标值，不该猜
_WEIRD = build_xml(SAMPLE, {'1': 777, '2': 1, '3': 1, '22': False})


def repair_case(xml, mode):
    p, cfg, dev = make_prefs(OffKeys=_REPAIR_OFF, OnKeys=_REPAIR_ON)
    dev.xml = xml
    return p, dev


def needs(xml, mode):
    p, _ = repair_case(xml, mode)
    return p.needs_repair(mode)


eq('已经是 OFF -> 不需要补写', needs(_ALREADY_OFF, False), {})
eq('已经是 ON  -> 不需要补写', needs(_ALREADY_ON, True), {})
eq('中间态(以德服人还开着) -> 只补 22', needs(_MIXED, False), {'22': False})
eq('中间态(倍率还关着) -> 只补 1/2/3',
   needs(_MIXED, True), {'1': 1000, '2': 1000, '3': 1000})
eq('陌生值 777 -> 不猜，返回空', needs(_WEIRD, False), {})

_p, _dev = repair_case(_MIXED, False)
_eq_repair = _p.repair(False)
check('repair 只改缺失的键并落盘', _eq_repair is True)
_d = parse(_dev.xml)
eq('repair 后 22 = false', _d['22'], ('boolean', 'false'))
eq('repair 后 1 仍是 1（没被重写）', _d['1'], ('int', '1'))
check('repair 时也停了游戏（否则写入会被覆盖）', 'app_stop' in _dev.calls, str(_dev.calls))
check('repair 后不主动启动游戏', 'app_start' not in _dev.calls, str(_dev.calls))

_p, _dev = repair_case(_ALREADY_OFF, False)
_dev.calls.clear()
eq('已达成目标时 repair 不做任何事', _p.repair(False), False)
_writes = [c for c in _dev.calls if c.startswith(('app_stop', 'app_start', 'adb_push'))]
eq('已达成目标时 repair 只读检测、不写不重启', _writes, [])

# ---------------------------------------------------------------- 4. 诊断
checker.header('4. diagnose')
p, cfg, dev = make_prefs()
info = p.diagnose()
check('diagnose 报告 root=True', info.get('root') is True, str(info))
check('diagnose 报告 exists=True', info.get('exists') is True)
check('diagnose 列出当前值', info.get('current_values') is not None, str(info.get('current_values')))
check('diagnose 报告游戏运行中', info.get('game_running') is True)

p, cfg, dev = make_prefs()
dev.running = False
info = p.diagnose()
check('diagnose 报告游戏未运行', info.get('game_running') is False, str(info))

p, cfg, dev = make_prefs()
dev.root = False
info = p.diagnose()
check('非 root 时 diagnose 仍可用', info.get('root') is False and 'error' not in info, str(info))

print(f'\n  真机 prefs 示例（当前设备实测内容）:\n{json.dumps(parse(SAMPLE), ensure_ascii=False)}')

sys.exit(1 if checker.summary() else 0)
