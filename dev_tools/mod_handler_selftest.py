"""
ModHandler 策略与状态机自检 —— 不接触设备，不需要模拟器。

覆盖：
  1. 策略解析（5 种范围）与「关闭/开启」互斥
  2. 用真实 module/config/argument/task.yaml 做全量覆盖度比对：
     敏感任务必须关；会进战斗的任务必须落在 关闭 或 开启 之一；不存在拼错的任务名
  3. 配置默认值与生成物一致性（argument.yaml / args.json / config_generated.py / template.json）
  4. 状态机：敏感任务关闭、常规任务启用、未列出任务沿用上次决定、幂等、外部改动纠偏、
     功能总开关关闭时仍强制关闭敏感任务的倍率
  5. ModHandler 其余分支：UI 后端、缺 key、读写盘
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mod_handler_testkit import (  # noqa: E402
    ROOT, Checker, FakeConfig, FakeDevice, default_of, install_stubs,
    load_argument, load_real_tasks,
)

install_stubs()

# 状态文件写到临时目录，别污染 config/mod_handler
TMP = tempfile.mkdtemp(prefix='alas_mod_state_')

from module.mod_handler import mod_handler as mh  # noqa: E402
from module.mod_handler.mod_prefs import ModPrefs  # noqa: E402
from module.config.config_updater import ConfigUpdater  # noqa: E402

mh.STATE_DIR = TMP

checker = Checker('ModHandler 自检')
check = checker.check
eq = checker.eq

SENSITIVE_DEFAULT = sorted(mh.GROUP_EXERCISE + mh.GROUP_META + mh.GROUP_RAID)

# ---------------------------------------------------------------- 1. 策略
checker.header('1. 策略解析')
print(f'  演习组 = {mh.GROUP_EXERCISE}')
print(f'  META组 = {mh.GROUP_META}')
print(f'  共斗组 = {mh.GROUP_RAID}')
print(f'  大舰队 = {mh.GROUP_GUILD}')
print(f'  常规组 = {len(mh.GROUP_NORMAL)} 项')

for option in ['disable_all_dangerous_task', 'disable_guild_and_dangerous',
               'disable_meta_and_exercise', 'disable_exercise', 'enable_all']:
    disabled, enabled = mh.resolve_policy(option)
    overlap = sorted(set(disabled) & set(enabled))
    duplicates = sorted({t for t in disabled if disabled.count(t) > 1})
    check(f'{option}: 关闭/开启无重叠', not overlap, f'overlap={overlap}')
    check(f'{option}: 关闭列表无重复', not duplicates, f'dup={duplicates}')

disabled, enabled = mh.resolve_policy('disable_all_dangerous_task')
for t in SENSITIVE_DEFAULT:
    check(f'默认策略关闭 `{t}`', t in disabled and t not in enabled)
for t in ['main', 'main2', 'main3', 'event', 'event_a', 'event_sp',
          'hard', 'daily', 'war_archives', 'gems_farming',
          'opsi_explore', 'opsi_daily', 'opsi_abyssal']:
    check(f'默认策略启用 `{t}`', t in enabled and t not in disabled)
check('默认策略启用 `guild`（需求：其他任务正常启用）', 'guild' in enabled)
eq('默认敏感任务集合', sorted(mh.sensitive_tasks()), SENSITIVE_DEFAULT)

# ---------------------------------------------------------------- 2. 覆盖度
checker.header('2. 与真实 ALAS 任务名对照')
real_tasks = load_real_tasks()
print(f'  task.yaml 任务数: {len(real_tasks)}')

LEGACY = {'sos', 'maritime_escort', 'event3',
          'c72_mystery_farming', 'c122_medium_leveling', 'c124_large_leveling'}

missing_sensitive = [t for t in SENSITIVE_DEFAULT if t not in real_tasks]
check('敏感任务名全部存在', not missing_sensitive, f'缺失={missing_sensitive}')

missing_normal = [t for t in mh.GROUP_NORMAL if t not in real_tasks and t not in LEGACY]
check('常规任务名全部存在（LEGACY 除外）', not missing_normal, f'缺失={missing_normal}')

missing_nochange = [t for t in mh.NO_CHANGE_TASKS if t not in real_tasks and t not in LEGACY]
check('不干预任务名全部存在（LEGACY 除外）', not missing_nochange, f'缺失={missing_nochange}')

# 三个列表必须覆盖所有真实任务，避免出现「既没决策也非明确不干预」的模糊任务
covered = set(SENSITIVE_DEFAULT) | set(mh.GROUP_NORMAL) | set(mh.NO_CHANGE_TASKS) | {'minigame'}
unknown = sorted(set(real_tasks) - covered)
check('所有真实任务都被显式分类', not unknown, f'未分类={unknown}')

# 会进战斗却被打上「不干预」标记的任务是隐患
BATTLE_HINTS = ('main', 'event_a', 'event_b', 'event_c', 'event_d', 'event_sp',
                'event', 'raid', 'coalition', 'exercise', 'ash', 'hard',
                'war_archives', 'gems_farming', 'daily', 'opsi_hazard', 'opsi_cross',
                'opsi_abyssal', 'opsi_obscure', 'opsi_stronghold', 'opsi_month', 'opsi_archive')
suspicious = [t for t in mh.NO_CHANGE_TASKS if t in BATTLE_HINTS]
check('「不干预」列表里没有战斗任务', not suspicious, f'可疑={suspicious}')

print('  当前版本的敏感任务:')
for t in SENSITIVE_DEFAULT:
    print(f'    {t}')

# ---------------------------------------------------------------- 3. 配置一致性
checker.header('3. 配置默认值与生成物一致性')

arg_yaml = os.path.join(ROOT, 'module', 'config', 'argument', 'argument.yaml')
args_json = os.path.join(ROOT, 'module', 'config', 'argument', 'args.json')
gen_py = os.path.join(ROOT, 'module', 'config', 'config_generated.py')
tpl_json = os.path.join(ROOT, 'config', 'template.json')

yaml_mod = load_argument('ModHandler')
eq('argument.yaml: ModHandler.Enabled', default_of(yaml_mod.get('Enabled')), True)
eq('argument.yaml: ModHandler.SensitiveTask 默认',
   default_of(yaml_mod.get('SensitiveTask')), 'disable_all_dangerous_task')
eq('argument.yaml: ModHandler.Backend 默认', default_of(yaml_mod.get('Backend')), 'prefs')
eq('argument.yaml: ModHandler.RestartTask 默认',
   default_of(yaml_mod.get('RestartTask')), 'always')

with open(args_json, encoding='utf-8') as f:
    args = json.load(f)

# ModHandler 必须是 args.json 里的「独立任务」，否则 GUI 点工具页的按钮会 KeyError
check('args.json: ModHandler 是独立任务', 'ModHandler' in args,
      f'顶层任务={sorted(args.keys())[-4:]}')
eq('args.json: ModHandler 任务含 ModHandler 参数组',
   sorted(args.get('ModHandler', {}).keys()), ['ModHandler', 'Storage'])
eq('args.json: ModHandler.Enabled 默认',
   args['ModHandler']['ModHandler']['Enabled']['value'], True)
eq('args.json: SensitiveTask 默认',
   args['ModHandler']['ModHandler']['SensitiveTask']['value'], 'disable_all_dangerous_task')
eq('args.json: SensitiveTask 选项',
   args['ModHandler']['ModHandler']['SensitiveTask']['option'],
   ['disable_all_dangerous_task', 'disable_guild_and_dangerous',
    'disable_meta_and_exercise', 'disable_exercise', 'enable_all'])

# 复现 module/webui/app.py:884 的遍历路径：守护总览点「设置」时走的就是这里
from module.config.deep import deep_iter as _deep_iter
_iter_ok = True
try:
    _groups = [g[0] for g, _ in _deep_iter(args['ModHandler'], depth=1) if g[0] != 'Storage']
except KeyError:
    _iter_ok = False
    _groups = []
check('GUI 守护总览能遍历 args["ModHandler"]（不再 KeyError）', _iter_ok, f'groups={_groups}')

# 菜单页签里必须有它，否则工具页看不到入口
with open(os.path.join(ROOT, 'module', 'config', 'argument', 'menu.json'),
          encoding='utf-8') as f:
    _menu = json.load(f)
check('menu.json 工具页含 ModHandler',
      'ModHandler' in _menu['Tool']['tasks'], str(_menu['Tool']['tasks']))

with open(gen_py, encoding='utf-8') as f:
    gen_src = f.read()
check('config_generated.py: ModHandler_Enabled = True', 'ModHandler_Enabled = True' in gen_src)
check('config_generated.py: ModHandler_Backend 存在', 'ModHandler_Backend' in gen_src)

with open(tpl_json, encoding='utf-8') as f:
    tpl = json.load(f)
eq('template.json: ModHandler.Enabled', tpl['ModHandler']['ModHandler']['Enabled'], True)

# 每一条 ModHandler 参数都要在四个地方齐全，避免 GUI 出现 KeyError
yaml_keys = set(yaml_mod.keys())
args_keys = set(args['ModHandler']['ModHandler'].keys())
check('args.json 与 argument.yaml 参数集合一致', yaml_keys == args_keys,
      f'仅yaml={sorted(yaml_keys - args_keys)} 仅args={sorted(args_keys - yaml_keys)}')
tpl_keys = set(tpl['ModHandler']['ModHandler'].keys())
check('template.json 与 argument.yaml 参数集合一致', yaml_keys == tpl_keys,
      f'差异={sorted(yaml_keys ^ tpl_keys)}')

for lang in ['zh-CN', 'en-US', 'ja-JP', 'zh-TW']:
    path = os.path.join(ROOT, 'module', 'config', 'i18n', f'{lang}.json')
    with open(path, encoding='utf-8') as f:
        i18n = json.load(f)
    missing = sorted(yaml_keys - set(i18n.get('ModHandler', {}).keys()))
    check(f'i18n {lang}: ModHandler 参数条目齐全', not missing, f'缺失={missing}')
    check(f'i18n {lang}: 有 Task.ModHandler（菜单/概览文案）',
          'ModHandler' in i18n.get('Task', {}),
          f'实际={i18n.get("Task", {}).get("ModHandler")}')

    # 防回归：webui 的 lang.t() 会对文案调用 .format()，
    # 文案里的单个 { } 会被当成占位符 → 打开配置页直接 KeyError('"1"')。
    # 直接复现 app.py:397 的取值路径，逐条跑一遍 .format()。
    _fmt_bad = []
    for _key in ['_info'] + list(yaml_keys):
        _entry = i18n.get('ModHandler', {}).get(_key)
        if not isinstance(_entry, dict):
            continue
        for _field in ('name', 'help'):
            _text = _entry.get(_field)
            if not isinstance(_text, str):
                continue
            try:
                _text.format()
            except Exception as _e:
                _fmt_bad.append(f'{_key}.{_field} -> {type(_e).__name__}: {_e}')
    check(f'i18n {lang}: ModHandler 文案可安全 .format()（花括号已转义）',
          not _fmt_bad, f'异常={_fmt_bad}')

# 防回归：args.json 里每个叶子节点都必须是含 type/value 的 dict。
# 曾经把 Storage 块少写一层嵌套，导致 GUI 打开配置页直接
# TypeError: string indices must be integers。
_bad_nodes = ['.'.join(keys) for keys, data in _deep_iter(args, depth=3)
              if not isinstance(data, dict) or 'value' not in data or 'type' not in data]
check('args.json 拓扑正常（无裸字符串/缺 value 的节点）', not _bad_nodes,
      f'异常={_bad_nodes[:8]}')

# 防回归：参数路径必须是 ModHandler.ModHandler.<参数>。
# 曾经写成 ModHandler.Enabled（少一层），deep_get 静默返回 default，
# 功能永远不生效 —— 而当时的假配置恰好也是错的，所以测试全绿。
_real_data = ConfigUpdater().read_file('template')  # 只读，不落盘
_real_data['ModHandler'] = {'ModHandler': dict(args['ModHandler']['ModHandler']),
                            'Storage': {'Storage': {}}}
for key, definition in args['ModHandler']['ModHandler'].items():
    _real_data['ModHandler']['ModHandler'][key] = definition['value']
_real_cfg = FakeConfig()
_real_cfg.data = _real_data
_real_handler = mh.ModHandler(config=_real_cfg, device='skip')
eq('真实配置形状下 enabled 能读到 Enabled',
   _real_handler.enabled, args['ModHandler']['ModHandler']['Enabled']['value'])
eq('真实配置形状下 backend 能读到 Backend',
   _real_handler.backend, args['ModHandler']['ModHandler']['Backend']['value'])
eq('真实配置形状下 sensitive_option 能读到 SensitiveTask',
   _real_handler.sensitive_option,
   args['ModHandler']['ModHandler']['SensitiveTask']['value'])
eq('真实配置形状下 off_keys 能读到 OffKeys',
   _real_handler.off_keys, mh.parse_key_values(
       args['ModHandler']['ModHandler']['OffKeys']['value']))
_real_prefs = ModPrefs(config=_real_cfg, device=None)
eq('真实配置形状下 ModPrefs.package 能读到 PackageName',
   _real_prefs.package, args['ModHandler']['ModHandler']['PackageName']['value'])
eq('真实配置形状下 ModPrefs.restart_policy 能读到 RestartTask',
   _real_prefs.restart_policy, args['ModHandler']['ModHandler']['RestartTask']['value'])

# ---------------------------------------------------------------- 4. 状态机
checker.header('4. 状态机（假设备）')


_handler_seq = [0]


def new_handler(**config_values):
    """
    造一个「后端总是写成功」的处理器，用来验证决策与状态机。

    每次都用独立的 config_name：状态文件是按 config_name 落盘的，
    共用名字会让上一个用例的缓存泄漏到下一个用例（表现为"已一致所以不动"）。
    """
    _handler_seq[0] += 1
    config_values.setdefault('config_name', f'selftest_{_handler_seq[0]}')
    cfg = FakeConfig(**config_values)
    dev = FakeDevice()
    handler = mh.ModHandler(config=cfg, device=dev)
    applied = []
    restarts = []

    def fake_set_multiplier(mode, restart=None):
        applied.append(mode)
        restarts.append(restart)
        return True   # 与真实后端一致：写入成功返回 True

    handler.set_multiplier = fake_set_multiplier
    handler.read_backend_state = lambda: None
    handler._applied = applied
    handler._restarts = restarts
    return handler, cfg, dev


# 4.1 敏感任务关闭、常规任务启用；状态已一致时不重复动作（这才是真实的调度序列）
SEQUENCE = [
    # (任务, 期望倍率, 是否应该产生一次动作)
    ('main', True, True),
    ('event_a', True, False),        # 已经是开
    ('hard', True, False),
    ('daily', True, False),
    ('opsi_explore', True, False),
    ('guild', True, False),          # 默认策略下大舰队正常启用
    ('exercise', False, True),       # 演习 -> 关
    ('main2', True, True),           # 回到主线 -> 开
    ('opsi_ash_beacon', False, True),    # META 信标 -> 关
    ('opsi_ash_assist', False, False),
    ('raid', False, False),          # 共斗 -> 关（已经是关）
    ('raid_daily', False, False),
    ('coalition', False, False),
    ('coalition_sp', False, False),
    ('war_archives', True, True),    # 回到常规 -> 开
    ('gems_farming', True, False),
]
h, cfg, dev = new_handler()
for task, want, should_apply in SEQUENCE:
    before = len(h._applied)
    changed = h.check_then_set(task)
    delta = len(h._applied) - before
    label = f'{"ON" if want else "OFF"}'
    check(f'{task:<18} -> {label:<3} '
          f'{"动作一次" if should_apply else "保持不动"}',
          changed is should_apply and delta == (1 if should_apply else 0)
          and (not should_apply or h._applied[-1] is want),
          f'changed={changed} delta={delta} applied={h._applied}')

# 4.2 幂等：同一个任务连续三次只动作一次
h, cfg, dev = new_handler()
first = h.check_then_set('exercise')
second = h.check_then_set('exercise')
third = h.check_then_set('exercise')
check('连续三次 exercise 只在第一次动作',
      first is True and second is False and third is False
      and h._applied == [False], f'applied={h._applied}')

# 4.2b 敏感任务必须要求重启（AlasGG 的 gg_reset 等价物），常规任务不强制
h, cfg, dev = new_handler()
h.check_then_set('exercise')
eq('敏感任务关倍率时要求重启', h._restarts, [True])
h.check_then_set('main')
eq('常规任务开倍率时不强制重启', h._restarts, [True, None])
h.check_then_set('opsi_ash_beacon')
eq('META 任务也要求重启', h._restarts, [True, None, True])
h.check_then_set('coalition')
eq('共斗任务也要求重启（状态已关则不再动作）', h._restarts, [True, None, True])

# 4.3 未列出任务沿用上次决定（共斗 -> commission -> main3 不能中途把倍率顶开）
h, cfg, dev = new_handler()
h.check_then_set('coalition')          # 关
h._applied.clear()
changed = h.check_then_set('commission')
check('未列出任务不改变状态', changed is False and h._applied == [],
      f'changed={changed} applied={h._applied}')
h.check_then_set('main3')
check('共斗后的常规任务才重新开启', h._applied == [True], f'applied={h._applied}')

# 4.4 从未决定过 -> 完全不动
h, cfg, dev = new_handler()
changed = h.check_then_set('commission')
check('无历史决定时未列出任务不动作', changed is False and h._applied == [])

# 4.5 外部改动被纠偏（设备说开着，策略要关）
h, cfg, dev = new_handler()
h.read_backend_state = lambda: True
changed = h.check_then_set('exercise')
check('设备实际开着倍率时纠正为关闭', changed and h._applied == [False],
      f'applied={h._applied}')

# 4.6 设备说关着，策略要开 -> 开回来
h, cfg, dev = new_handler()
h.read_backend_state = lambda: False
changed = h.check_then_set('main')
check('设备实际关着倍率时恢复为开启', changed and h._applied == [True],
      f'applied={h._applied}')

# 4.7 功能总开关关闭
h, cfg, dev = new_handler(Enabled=False)
changed = h.check_then_set('exercise')
check('Enabled=False 时不动作', changed is False and h._applied == [])

h, cfg, dev = new_handler(Enabled=False)
h.read_backend_state = lambda: True
changed = h.check_then_set('raid')
check('Enabled=False 但敏感任务里倍率开着 -> 强制关闭', changed and h._applied == [False],
      f'applied={h._applied}')

h, cfg, dev = new_handler(Enabled=False)
h.read_backend_state = lambda: True
changed = h.check_then_set('main')
check('Enabled=False 时常规任务不主动开启', changed is False and h._applied == [])

# 4.8 显式 enable_all：连敏感任务都开
h, cfg, dev = new_handler(SensitiveTask='enable_all')
changed = h.check_then_set('exercise')
check('enable_all 时演习也开启', changed and h._applied == [True])

# 4.9 force
h, cfg, dev = new_handler()
h._last_want = False
changed = h.check_then_set('exercise', force=True)
check('force=True 时即使状态一致也执行', changed and h._applied == [False])

# 4.10 启动纠偏
h, cfg, dev = new_handler()
h.set_state(False)                       # 上次决定：关
h.read_backend_state = lambda: True      # 用户手动开回来了
changed = h.check_on_startup()
check('启动纠偏：外部开启被关回', changed and h._applied == [False], f'applied={h._applied}')

h, cfg, dev = new_handler()
h.set_state(True)
h.read_backend_state = lambda: True
h._applied.clear()
changed = h.check_on_startup()
check('启动纠偏：状态一致时不动作', changed is False and h._applied == [])

h, cfg, dev = new_handler()
h.set_state(False)
h.read_backend_state = lambda: None      # 读不到设备
h._applied.clear()
changed = h.check_on_startup()
check('启动纠偏：读不到设备时不动', changed is False and h._applied == [])

# 4.11 状态落盘 / 读回
h, cfg, dev = new_handler(config_name='selftest_cfg')
h.check_then_set('exercise')
eq('落盘后的 get_state', h.get_state(), False)
check('落盘后的 get_decided', h.get_decided() is True)
state_file = os.path.join(TMP, 'mod_state_selftest_cfg.json')
check('状态文件已生成', os.path.exists(state_file), state_file)
with open(state_file, encoding='utf-8') as f:
    eq('状态文件内容', json.load(f), {'multiplier_on': False, 'decided': True})

# 4.12 current_state 的三级回退
h, cfg, dev = new_handler()
h.read_backend_state = lambda: False
eq('设备优先', h.current_state(), (False, 'device'))
h.read_backend_state = lambda: None
h.set_state(True)
eq('设备读不到时用缓存', h.current_state(), (True, 'cache'))
h2, _, _ = new_handler(config_name='empty_cfg')
eq('都没有时为 unknown', h2.current_state(), (None, 'unknown'))

# 4.13 UI 后端也要能读状态
h, cfg, dev = new_handler(Backend='ui')
backend = h._backend
check('Backend=ui 时实例化 ModUi', type(backend).__name__ == 'ModUi', type(backend).__name__)
h2, _, _ = new_handler(Backend='prefs')
check('Backend=prefs 时实例化 ModPrefs', type(h2._backend).__name__ == 'ModPrefs',
      type(h2._backend).__name__)

# 4.14 缺 key 时只提示不写设备（但仍记住本次决定）
h, cfg, dev = new_handler(OffKeys='', OnKeys='')
check('keys_configured=False', h.keys_configured is False)
h._applied.clear()
changed = h.check_then_set('exercise')
check('缺 key 时不写设备', changed is False and h._applied == [])
eq('缺 key 时仍记住本次决定', h.get_state(), False)
check('configured=True（默认）', new_handler()[0].keys_configured is True)

# 4.15 后端写失败时如实上报，不谎报成功
h, cfg, dev = new_handler()
h.set_multiplier = lambda mode, restart=None: False      # 后端没写成
h.read_backend_state = lambda: None
changed = h.check_then_set('exercise')
check('后端未改动时 check_then_set 返回 False', changed is False)

# 4.16 显式传入 device 时必须保留它（曾经被无条件覆盖成 None，
#      导致所有 adb 操作都对 None 设备执行）
_dev = FakeDevice()
h = mh.ModHandler(config=FakeConfig(), device=_dev)
check('显式 device 被保留', h.device is _dev, f'device={h.device}')
check('显式 device 时 _device_free=False', h._device_free is False)

# 4.17 device='skip' 时完全不碰设备
h = mh.ModHandler(config=FakeConfig(), device='skip')
check("device='skip' 时 _device_free=True", h._device_free is True)
eq("device='skip' 时 describe_state 明确报告未绑定设备",
   h.describe_state().get('option'), 'unknown')
check("device='skip' 时 describe_state 不抛异常", isinstance(h.describe_state(), dict))

# ---------------------------------------------------------------- 5. 关键值解析
checker.header('5. OffKeys / OnKeys 解析')
cases = [
    ('1=1,2=1,3=1', {'1': 1, '2': 1, '3': 1}),
    ('{"1": 1000, "2": 1000}', {'1': 1000, '2': 1000}),
    ('22=false,23=false', {'22': False, '23': False}),
    ('', {}),
    ('  1 = 0 ; 2 = 0 ', {'1': 0, '2': 0}),
    ('1=1000,2=1.5,3=abc', {'1': 1000, '2': 1.5, '3': 'abc'}),
    ('-1=true,-3=2', {'-1': True, '-3': 2}),
]
for text, expect in cases:
    eq(f'parse_key_values({text!r})', mh.parse_key_values(text), expect)

# ---------------------------------------------------------------- 6. 模块与钩子
checker.header('6. 调度器钩子与模块导入')
with open(os.path.join(ROOT, 'alas.py'), encoding='utf-8') as f:
    alas_src = f.read()
check('alas.py 导入 ModHandler',
      'from module.mod_handler.mod_handler import ModHandler' in alas_src)
check('alas.py 调用 check_then_set', 'check_then_set' in alas_src)
check('alas.py 调用 check_on_startup', 'check_on_startup' in alas_src)
check('钩子在 Start task 之前',
      alas_src.index('check_then_set') < alas_src.index('Scheduler: Start task'))
check('钩子被 try/except 包住，失败不拖垮调度',
      'failed to apply multiplier policy' in alas_src)

# 防回归：webui app.py 的结构。曾经因为编辑失误把 set_group 的
# @use_scope("groups") 装饰器吃掉、同时丢掉 refresh_mod_handler_state 调用，
# 结果「所有任务配置页全部空白」。这里用源码级断言盯住这两处。
with open(os.path.join(ROOT, 'module', 'webui', 'app.py'), encoding='utf-8') as f:
    app_src = f.read()
check('app.py: set_group 仍带 @use_scope("groups") 装饰器',
      '@use_scope("groups")\n    def set_group(' in app_src)
check('app.py: alas_set_group 仍调用 refresh_mod_handler_state',
      'self.refresh_mod_handler_state(config, task)' in app_src)
_app_group_body = app_src.split('def alas_set_group(', 1)[-1].split('\n    def ', 1)[0]
check('app.py: alas_set_group 里保留了参数组遍历（否则页面会空白）',
      'deep_iter(self.ALAS_ARGS[task], depth=1)' in _app_group_body)
check('app.py: 状态读取失败时会写 warning 日志，便于定位',
      'Failed to read modifier state' in app_src)
check('app.py: 用纯 adb 只读通道读状态（不 import PIL / module.device）',
      'describe_state_readonly' in app_src)
check('app.py: 不再依赖临时替换 PIL（旧方案在 GUI 里不稳）',
      'remove_fake_pil_module' not in app_src)
with open(os.path.join(ROOT, 'module', 'mod_handler', 'mod_prefs.py'), encoding='utf-8') as f:
    _prefs_src = f.read()
with open(os.path.join(ROOT, 'module', 'mod_handler', 'mod_handler.py'), encoding='utf-8') as f:
    _handler_src = f.read()
check('mod_prefs.py: PIL 不可用时可降级导入（否则 GUI 里 import 就炸）',
      'except ImportError' in _prefs_src and '_ModuleBaseStub' in _prefs_src)
check('mod_handler.py: 同样有降级导入',
      'except ImportError' in _handler_src and '_ModuleBaseStub' in _handler_src)

# 防回归（真踩过）：GUI 状态栏走的 describe_state_readonly 不能实例化 ModPrefs。
# ModPrefs 是 ModuleBase 子类，__init__ 会做 OCR 导入/构造 Device，
# 在 webui 假 PIL 环境抛 AttributeError，被空 except 吞掉后显示成"读不到状态"。
_without_body = _prefs_src.split('def describe_state_readonly(', 1)[-1]
_factory_body = _without_body.split('\ndef ', 1)[0]
# 只禁「实例化」：ModPrefs.parse(...) 是静态方法，允许
check('describe_state_readonly 不实例化 ModPrefs（避开 ModuleBase.__init__）',
      not any(l.strip().startswith('ModPrefs(') or l.strip().startswith('prefs = ModPrefs(')
              for l in _factory_body.splitlines()))
check('describe_state_readonly 不构造 Device',
      'Device(' not in _factory_body)
check('app.py: 渲染计数写进日志，页面空白时可直接定位',
      'groups rendered' in app_src and 'nothing rendered' in app_src)

for mod in ['module.mod_handler.mod_handler', 'module.mod_handler.mod_prefs',
            'module.mod_handler.mod_ui']:
    try:
        __import__(mod)
        check(f'可导入 {mod}', True)
    except Exception as e:
        check(f'可导入 {mod}', False, f'{type(e).__name__}: {e}')

print('\n清理临时状态目录')
shutil.rmtree(TMP, ignore_errors=True)

sys.exit(1 if checker.summary() else 0)
