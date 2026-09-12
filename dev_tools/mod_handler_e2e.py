"""
端到端集成自检 —— 从调度器视角跑一整轮任务，验证倍率开关真的落到「设备」上。

与 mod_handler_selftest.py 的区别：
  * 前者把 set_multiplier 拦截掉，只验证「什么时候决定」
  * 本脚本不拦截任何东西，走完整链路：
        ModHandler.check_then_set -> ModPrefs.set_multiplier
        -> 读 prefs XML -> 停游戏 -> 推送改写后的 XML -> 游戏保持关闭
        （写完不主动拉起，ALAS 发现游戏没跑会自动排 Restart 任务）
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

    # _su 直接用 testkit 里那套「段内 && 串联、段间 ; 分隔」的最小 shell 模拟：
    # ModPrefs.write_raw 现在靠命令链末尾的 echo 标记判定成功，自己再实现一份
    # 很容易漏掉标记，把成功的写入误判成失败。

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
check('敏感任务：停游戏 -> 推送 -> 游戏保持关闭（交给 ALAS 的 Restart 任务）',
      dev.writes == ['app_stop', f'adb_push:{TMP_REMOTE}'], str(dev.writes))

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
check('常规任务恢复：游戏没在跑，直接推送，写完保持关闭',
      dev.writes == [f'adb_push:{TMP_REMOTE}'], str(dev.writes))

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
checker.header('5. 启动检查（模拟「用户手动开回倍率」）')
h, cfg, dev = build('e2e_startup')
h.check_then_set('exercise')                 # 停在敏感任务：倍率关
check('执行后设备倍率关闭', dev.multiplier_on is False)

# 用户手动把倍率开回去（直接改「设备」上的 XML，模拟动悬浮窗）
dev.xml = ModPrefs.build_xml(dev.xml, {'1': 1000, '2': 1000, '3': 1000, '22': True})
check('用户手动开启后设备倍率开着', dev.multiplier_on is True)

# 假设 ALAS 重启：启动检查只告警、不写设备，真正的动作交给下一个任务
h2, cfg2, dev2 = build('e2e_startup', xml=dev.xml)
eq('新进程读到的缓存', h2.get_state(), False)
dev2.calls.clear()
changed = h2.check_on_startup()
check('启动检查发现外部改动只告警、不写设备',
      changed is False and dev2.writes == [], f'writes={dev2.writes}')
check('设备保持用户改动后的状态', dev2.multiplier_on is True)

# 下一个任务是敏感任务：按策略把倍率关掉，只写这一次
changed = h2.check_then_set('exercise')
check('敏感任务把倍率关回（只写一次）',
      changed is True and dev2.multiplier_on is False, f'writes={dev2.writes}')
check('完整流程：停游戏 -> 推送 -> 保持关闭',
      dev2.writes == ['app_stop', f'adb_push:{TMP_REMOTE}'], str(dev2.writes))

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
checker.header('7. 非 root 时后端不可用：确认不了已关 -> 停机推送（fail-closed）')
# ★ 语义对齐 2026-09-12 19:04（9ebb55e03）：该关没关且回读确认不了「已关」
#   （None）或确认仍开着 -> 一律停机推送。本用例曾在语义切换时被漏改
#   （第七轮审查 N7-2），导致一键自检的 e2e 套件长期报红。
h, cfg, dev = build('e2e_noroot')
dev.root = False
dev.calls.clear()
try:
    h.check_then_set('exercise')
    check('非 root 确认不了倍率已关 -> 停机推送', False, '没有抛异常')
except mh.RequestHumanTakeover:
    check('非 root 确认不了倍率已关 -> 停机推送', True)
check('非 root 时设备 XML 未被改动', dev.multiplier_on is True)
eq('非 root 时未停游戏', [c for c in dev.calls if c.startswith('app_')], [])

# ---------------------------------------------------------------- 8
checker.header('8. Enable_all 策略')
h, cfg, dev = build('e2e_enableall', SensitiveTask='enable_all')
dev.xml = ModPrefs.build_xml(dev.xml, {'1': 1, '2': 1, '3': 1, '22': False})
check('起始倍率关闭', dev.multiplier_on is False)
changed = h.check_then_set('exercise')
check('enable_all 时演习也开倍率', changed is True and dev.multiplier_on is True)

# ---------------------------------------------------------------- 9
checker.header('9. 跨月每日（opsi_cross_month）时序')
# 该任务每月最后一天 23:50 触发，进入后 in-process 等到 00:00 的 OpSi 重置，
# 期间一直待在同一个任务里（不重启、不退出任务），跑完再 task_stop()。
# 属于常规任务，应当「开倍率」；任务内部的等待期我们不介入。
h, cfg, dev = build('e2e_cross_month')
dev.xml = ModPrefs.build_xml(dev.xml, {'1': 1, '2': 1, '3': 1, '22': False})
check('前置：设备上倍率关闭（敏感任务刚跑完）', dev.multiplier_on is False)

dev.calls.clear()
changed = h.check_then_set('opsi_cross_month')
check('跨月每日会把倍率打开（不在敏感项里）',
      changed is True and dev.multiplier_on is True, f'writes={dev.writes}')

# 任务内部等到 00:00 重置、继续跑每日/深渊/隐秘 —— 期间不会有新的任务边界，
# 所以不会产生任何多余动作
dev.calls.clear()
inside = h.check_then_set('opsi_cross_month')
check('任务等待期间不产生动作', inside is False and dev.writes == [], str(dev.writes))
check('设备倍率保持开启', dev.multiplier_on is True)

# 跑完 task_stop()，调度器继续下一个任务
dev.calls.clear()
changed = h.check_then_set('main2')
check('跨月任务之后的常规任务不重复动作',
      changed is False and dev.writes == [], str(dev.writes))

dev.calls.clear()
changed = h.check_then_set('exercise')
check('紧接演习仍能正确关闭倍率',
      changed is True and dev.multiplier_on is False, f'writes={dev.writes}')

# ---------------------------------------------------------------- 10
checker.header('10. 实例启动、游戏还没起来：第一个任务不能把实例停掉')
# 用户实际踩到的场景：模拟器开着，游戏还没起来，实例的第一个任务就走到
# check_then_set。Backend 用默认的 overlay（生产配置），设备上倍率是开的。
# 旧行为：overlay 硬走悬浮窗 -> show() 拒绝（游戏没跑，am startservice 会新起
# 进程把游戏搞崩）-> OverlayFallbackPrefs 默认关 -> 返回 False
# -> ModHandler 回读确认「倍率还开着」-> RequestHumanTakeover，实例一启动就停。
_cold_cfg = FakeConfig(
    config_name='e2e_cold_start',
    Enabled=True,
    Backend='overlay',
    OffKeys=OFF_KEYS,
    OnKeys=ON_KEYS,
    SensitiveTask='disable_all_dangerous_task',
    OverlayFallbackPrefs=False,      # 生产默认值：降级开关是关的
)
_cold_dev = SimulatedDevice()        # 设备上倍率开着
_cold_dev.running = False            # 模拟器开着，但游戏还没起来
h10 = mh.ModHandler(config=_cold_cfg, device=_cold_dev)
check('冷启动时后端确实是 overlay', type(h10._backend).__name__ == 'ModOverlay',
      type(h10._backend).__name__)
_cold_dev.calls.clear()
try:
    changed = h10.check_then_set('exercise')      # 第一个任务就是演习（要关倍率）
    check('游戏未运行时第一个敏感任务不停机', True)
except mh.RequestHumanTakeover as e:
    check('游戏未运行时第一个敏感任务不停机', False, f'实例被停掉了: {e}')
else:
    check('游戏未运行时第一个敏感任务确实关掉了倍率',
          changed is True and _cold_dev.multiplier_on is False,
          f'changed={changed} on={_cold_dev.multiplier_on}')
    eq('游戏未运行时不需要停游戏', [c for c in _cold_dev.calls if c.startswith('app_')], [])
    eq('游戏未运行时完全不碰悬浮窗 Service',
       [c for c in _cold_dev.calls if 'service' in c], [])
    check('游戏未运行时的关闭确实落到了设备上（推送了 XML）',
          any(c.startswith('adb_push:') for c in _cold_dev.calls), str(_cold_dev.calls))

# 同一场景、但用户关掉了 Alas.Error.HandleError。prefs 后端与该配置互斥（要靠
# app_stop 停游戏才写得安全），可冷启动时根本没有 app_stop 可调用 —— 所以互斥
# 判定不能拦这条路，否则这类用户会以另一种配置复现同一个「一启动就停机」。
_cold2_cfg = FakeConfig(
    config_name='e2e_cold_start_nohandle',
    Enabled=True,
    Backend='overlay',
    OffKeys=OFF_KEYS,
    OnKeys=ON_KEYS,
    SensitiveTask='disable_all_dangerous_task',
    OverlayFallbackPrefs=False,
    Error_HandleError=False,         # ★ 与 app_stop 互斥的那个配置
)
_cold2_dev = SimulatedDevice()
_cold2_dev.running = False
h10b = mh.ModHandler(config=_cold2_cfg, device=_cold2_dev)
_cold2_dev.calls.clear()
try:
    changed = h10b.check_then_set('exercise')
    check('HandleError 关闭时冷启动第一个任务也不停机', True)
except mh.RequestHumanTakeover as e:
    check('HandleError 关闭时冷启动第一个任务也不停机', False, f'实例被停掉了: {e}')
else:
    check('HandleError 关闭时倍率确实关掉了',
          changed is True and _cold2_dev.multiplier_on is False,
          f'changed={changed} on={_cold2_dev.multiplier_on}')
    eq('HandleError 关闭时也压根没有 app_stop 需要调',
       [c for c in _cold2_dev.calls if c.startswith('app_')], [])

# 游戏被 ALAS 的 Restart 任务拉起来之后：若悬浮窗不可用（本进程内已被崩溃保护
# 停用），打开 OverlayFallbackPrefs 仍能通过 prefs 写回去 —— 代价是一次游戏重启。
# 这里把 _disabled 直接置上，等价于「悬浮窗已知不可用」，同时省掉 show() 的 10s 超时。
_cold_cfg.set(OverlayFallbackPrefs=True)
_cold_dev.running = True
h10._backend._disabled = True
_cold_dev.calls.clear()
changed = h10.check_then_set('main')
check('游戏起来后常规任务把倍率开回来（走 prefs 降级）',
      changed is True and _cold_dev.multiplier_on is True, f'writes={_cold_dev.writes}')
check('降级路径按预期停了一次游戏（这正是它要付的代价）',
      'app_stop' in _cold_dev.calls, str(_cold_dev.calls))

shutil.rmtree(STATE_TMP, ignore_errors=True)
sys.exit(1 if checker.summary() else 0)
