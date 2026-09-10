"""
端到端集成自检 —— 从调度器视角跑一整轮任务，验证倍率开关真的落到「设备」上。

与 mod_handler_selftest.py 的区别：
  * 前者把 set_multiplier 拦截掉，只验证「什么时候决定」
  * 本脚本不拦截任何东西，走完整链路：
        ModHandler.check_then_set -> ModPrefs.set_multiplier
        -> 读 prefs XML -> 停游戏 -> 推送改写后的 XML -> 重启游戏
    设备侧用真机抓下来的 prefs 内容做假设备，因此可以断言每一步之后的 XML。

顺序上模拟 alas.py 的调度循环：启动纠偏一次 -> 每个任务开始前 check_then_set。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mod_handler_testkit import (  # noqa: E402
    ROOT, Checker, FakeConfig, FakeDevice, install_stubs,
)

install_stubs()

from module.mod_handler import mod_handler as mh  # noqa: E402
from module.mod_handler.mod_prefs import ModPrefs  # noqa: E402

STATE_TMP = tempfile.mkdtemp(prefix='alas_mod_e2e_')
mh.STATE_DIR = STATE_TMP

checker = Checker('端到端集成自检')
check = checker.check
eq = checker.eq

# 真机 127.0.0.1:16416 上抓下来的原始内容（改版客户端的悬浮窗开关）
REAL_PREFS = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
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

# 倍率用数值 key（1/2/3 = 1000 -> 1），另外再叠一个布尔开关，覆盖两种类型
OFF_KEYS = '1=1,2=1,3=1,22=false'
ON_KEYS = '1=1000,2=1000,3=1000,22=true'

PREF_PATH = ('/data/data/com.bilibili.azurlane/shared_prefs/'
             'com.bilibili.azurlane_preferences.xml')
TMP_REMOTE = '/data/local/tmp/_alas_mod_prefs.xml'


class SimulatedDevice(FakeDevice):
    """
    一个「真的会变」的假设备：把 prefs 存在内存里，
    按 ModPrefs 发出的 adb 命令读改写，并记录游戏进程的停/启。
    """

    def __init__(self, xml=REAL_PREFS, root=True, running=True):
        super().__init__(root=root)
        self.xml = xml
        self.running = running

    # --- 记录 + 执行 ---
    def adb_shell(self, cmd, timeout=10, **kwargs):
        recorded = super().adb_shell(cmd, timeout=timeout, **kwargs)
        if isinstance(recorded, str) and recorded.startswith('su -c'):
            inner = recorded[len('su -c'):].strip().strip("'").replace("'\\''", "'")
            return self._su(inner)
        return recorded

    def _su(self, inner):
        inner = inner.strip()
        if not self.root:
            return 'uid=1000(u0_a46) gid=1000(u0_a46)'
        if inner == 'id':
            return 'uid=0(root) gid=0(root)'
        if inner.startswith('cat '):
            return self.xml if self.xml is not None else ''
        if inner.startswith('stat '):
            return '10046 10046 660'
        if inner.startswith('cp '):
            # cp TMP PREF && chown ... && chmod ... && restorecon ... ; rm -f TMP
            self.xml = self.pushed[TMP_REMOTE]
            return ''
        return ''

    def adb_push(self, local, remote):
        super().adb_push(local, remote)

    def app_stop(self):
        # Device.app_stop 在 HandleError 关闭时会抛 RequestHumanTakeover
        super().app_stop()

    def app_start(self):
        super().app_start()

    # --- 断言辅助 ---
    @property
    def parsed(self):
        return ModPrefs.parse(self.xml)

    @property
    def multiplier_on(self):
        return all(self.parsed[str(k)][1] == str(v) for k, v in
                   {'1': 1000, '2': 1000, '3': 1000, '22': 'true'}.items())

    @property
    def writes(self):
        return [c for c in self.calls if c.startswith(('app_stop', 'app_start', 'adb_push'))]


def build(config_name, xml=REAL_PREFS, **overrides):
    values = {
        'Enabled': True,
        'Backend': 'prefs',
        'OffKeys': OFF_KEYS,
        'OnKeys': ON_KEYS,
        'RestartGame': True,
        'SensitiveTask': 'disable_all_dangerous_task',
    }
    values.update(overrides)
    cfg = FakeConfig(config_name=config_name, **values)
    dev = SimulatedDevice(xml=xml)
    handler = mh.ModHandler(config=cfg, device=dev)
    # 让 ModHandler 用同一个假设备
    handler._backend_obj = ModPrefs(config=cfg, device=dev)
    return handler, cfg, dev


# ---------------------------------------------------------------- 1
checker.header('1. 初始状态')
h, cfg, dev = build('e2e_basic')
check('设备初始倍率开着', dev.multiplier_on is True)
eq('ModHandler 读回设备状态', h.current_state(), (True, 'device'))
eq('OffKeys 解析', h.off_keys, {'1': 1, '2': 1, '3': 1, '22': False})

# ---------------------------------------------------------------- 2
checker.header('2. 敏感任务 -> 真正写进设备')
dev.calls.clear()
first = h.check_then_set('exercise')
check('第一次敏感任务：写了一次并关掉倍率',
      first is True and dev.multiplier_on is False, f'writes={dev.writes}')
check('停游戏 -> 推送 -> 重启游戏',
      dev.writes == ['app_stop', f'adb_push:{TMP_REMOTE}', 'app_start'], str(dev.writes))

for task in ['exercise', 'opsi_ash_beacon', 'opsi_ash_assist', 'raid', 'raid_daily',
             'coalition', 'coalition_sp']:
    dev.calls.clear()
    changed = h.check_then_set(task)
    check(f'{task:<18} 倍率保持关闭且不重复写', changed is False and dev.writes == []
          and dev.multiplier_on is False, f'changed={changed} writes={dev.writes}')

# ---------------------------------------------------------------- 3
checker.header('3. 常规任务 -> 倍率写回')
dev.calls.clear()
changed = h.check_then_set('main')
check('第一个常规任务：写了一次并开启倍率',
      changed is True and dev.multiplier_on is True, f'writes={dev.writes}')
check('恢复同样走完整流程',
      dev.writes == ['app_stop', f'adb_push:{TMP_REMOTE}', 'app_start'], str(dev.writes))

for task in ['event_a', 'hard', 'daily', 'opsi_explore', 'guild', 'war_archives']:
    dev.calls.clear()
    changed = h.check_then_set(task)
    check(f'{task:<18} 倍率保持开启且不重复写', changed is False and dev.writes == []
          and dev.multiplier_on is True, f'changed={changed} writes={dev.writes}')

# ---------------------------------------------------------------- 4
checker.header('4. 完整任务序列（设备状态逐步演化）')
h, cfg, dev = build('e2e_sequence')
SEQUENCE = [
    ('Restart', 'restart', True, False),          # 通用任务，不干预
    ('Main', 'main', True, False),                # 已经是开 -> 不动
    ('Exercise', 'exercise', False, True),        # 演习 -> 关
    ('Commission', 'commission', False, False),   # 后勤 -> 沿用「关」
    ('OpsiAshBeacon', 'opsi_ash_beacon', False, False),
    ('OpsiExplore', 'opsi_explore', True, True),  # 大世界 -> 开
    ('Coalition', 'coalition', False, True),      # 共斗 -> 关
    ('Dorm', 'dorm', False, False),               # 宿舍 -> 沿用「关」
    ('RaidDaily', 'raid_daily', False, False),
    ('Main3', 'main3', True, True),
]
ok = True
for camel, snake, want_on, should_write in SEQUENCE:
    dev.calls.clear()
    changed = h.check_then_set(snake)
    wrote = len(dev.writes) > 0
    state_ok = dev.multiplier_on is want_on
    ok = ok and (changed is should_write) and (wrote is should_write) and state_ok
    check(f'{camel:<16} -> {"ON " if want_on else "OFF"} '
          f'{"写入" if should_write else "不动"}',
          (changed is should_write) and (wrote is should_write) and state_ok,
          f'changed={changed} writes={dev.writes} on={dev.multiplier_on}')

# 全程只应该在状态切换时重启游戏
eq('最终设备状态', dev.multiplier_on, True)

# ---------------------------------------------------------------- 5
checker.header('5. 启动纠偏（模拟「用户手动开回倍率」）')
h, cfg, dev = build('e2e_startup')
h.check_then_set('exercise')                 # 停在敏感任务：倍率关
check('执行后设备倍率关闭', dev.multiplier_on is False)

# 用户手动把倍率开回去（直接改「设备」上的 XML，模拟动悬浮窗）
dev.xml = ModPrefs.build_xml(dev.xml, {'1': 1000, '2': 1000, '3': 1000, '22': True})
check('用户手动开启后设备倍率开着', dev.multiplier_on is True)

# 假设 ALAS 重启并直接跑常规任务：没有纠偏的话会一直开着倍率
h2, cfg2, dev2 = build('e2e_startup', xml=dev.xml)
eq('新进程读到的缓存', h2.get_state(), False)
dev2.calls.clear()
changed = h2.check_on_startup()
check('启动纠偏把倍率关回', changed is True and dev2.multiplier_on is False,
      f'writes={dev2.writes}')
check('纠偏同样走完整流程',
      dev2.writes == ['app_stop', f'adb_push:{TMP_REMOTE}', 'app_start'], str(dev2.writes))

# ---------------------------------------------------------------- 6
checker.header('6. 未配置 key 时不产生任何设备动作')
h, cfg, dev = build('e2e_nokeys', OffKeys='', OnKeys='')
dev.calls.clear()
changed = h.check_then_set('exercise')
check('无 key 时不报告变更', not changed, f'changed={changed}')
eq('无 key 时不产生写动作', dev.writes, [])
check('无 key 时设备倍率保持原样', dev.multiplier_on is True)
check('无 key 时 keys_configured=False', h.keys_configured is False)

# ---------------------------------------------------------------- 7
checker.header('7. 非 root 时不破坏设备状态')
h, cfg, dev = build('e2e_noroot')
dev.root = False
dev.calls.clear()
try:
    h.check_then_set('exercise')
    check('非 root 时异常被 check_then_set 抛出', False, '没有抛异常')
except RuntimeError as e:
    check('非 root 时抛出可读错误', 'root' in str(e), str(e))
check('非 root 时设备 XML 未被改动', dev.multiplier_on is True)
eq('非 root 时未停游戏', [c for c in dev.calls if c.startswith('app_')], [])

# ---------------------------------------------------------------- 8
checker.header('8. Enable_all 策略')
h, cfg, dev = build('e2e_enableall', SensitiveTask='enable_all')
dev.xml = ModPrefs.build_xml(dev.xml, {'1': 1, '2': 1, '3': 1, '22': False})
check('起始倍率关闭', dev.multiplier_on is False)
changed = h.check_then_set('exercise')
check('enable_all 时演习也开倍率', changed is True and dev.multiplier_on is True)

shutil.rmtree(STATE_TMP, ignore_errors=True)
sys.exit(1 if checker.summary() else 0)
