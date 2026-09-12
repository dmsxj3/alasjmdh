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
eq('argument.yaml: ModHandler.Backend 默认', default_of(yaml_mod.get('Backend')), 'overlay')
check('argument.yaml: RestartTask 已删除（写完不重启，交给 ALAS 的 Restart 任务）',
      'RestartTask' not in yaml_mod)

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
check('args.json: RestartTask 已删除（写完不重启，交给 ALAS 的 Restart 任务）',
      'RestartTask' not in args['ModHandler']['ModHandler'])
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


def _norm(value):
    """None 与 '' 视为等价空值（yaml 用空串、template.json 用 null 表达「未配」）。"""
    return '' if value is None else value


# 防回归（真踩过）：template.json 曾把 OverlayFallbackPrefs 留成 true，
# 而 argument.yaml 已改 false —— 新建实例会拿到与声明相反的默认值。
# 集合一致比不出取值差异，这里逐参数比对默认值。
_value_drift = [
    f'{k}: tpl={_norm(default_of(tpl["ModHandler"]["ModHandler"].get(k)))!r} '
    f'yaml={_norm(default_of(yaml_mod.get(k)))!r}'
    for k in sorted(yaml_keys & tpl_keys)
    if _norm(default_of(tpl['ModHandler']['ModHandler'].get(k)))
    != _norm(default_of(yaml_mod.get(k)))
]
check('template.json 与 argument.yaml 默认值无漂移', not _value_drift,
      f'漂移={_value_drift}')

# 防回归：代码里的「配置缺键时的缺省值」也必须与配置源一致。
# mod_overlay.fallback_enabled 曾经写成 default=True，而四处配置源全是 false ——
# 老实例（config 里没有这个键）或手工改过的 config 会静默变成「允许为了关倍率
# 重启游戏」，与模块 docstring 写的「默认关」和用户偏好都相反。
from module.mod_handler.mod_overlay import ModOverlay as _ModOverlay  # noqa: E402

eq('overlay.fallback_enabled 的缺键缺省值 = 配置源默认值',
   _ModOverlay(config=FakeConfig(), device=FakeDevice()).fallback_enabled,
   bool(default_of(yaml_mod.get('OverlayFallbackPrefs'))))

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

    # 防回归（真踩过）：帮助文本里写成了 "\\n"（JSON 里转义成**字面反斜杠 + n**），
    # GUI 上会原样显示 "\n" 而不是换行；同处还混用了 markdown 的 ** 粗体，
    # 而 webui 不渲染 markdown，星号会直接露出来。
    _esc_bad = []
    _bold_bad = []

    def _scan(prefix, node):
        if isinstance(node, dict):
            for k, v in node.items():
                _scan(f'{prefix}.{k}' if prefix else k, v)
        elif isinstance(node, str):
            if (chr(92) + 'n') in node:
                _esc_bad.append(prefix)
            if '**' in node:
                _bold_bad.append(prefix)

    _scan('', i18n.get('ModHandler', {}))
    check(f'i18n {lang}: 帮助文本没有字面反斜杠 n（应为真换行）',
          not _esc_bad, f'异常={_esc_bad}')
    check(f'i18n {lang}: 帮助文本没有 markdown 粗体星号（webui 不渲染）',
          not _bold_bad, f'异常={_bold_bad}')

# 防回归：ModHandler 参数组只能出现在 ModHandler 任务下。
# 曾经它被挂在 GameManager 下（后来拆成独立任务），但 args.json 打补丁时
# 漏删旧的那份，导致 GUI 上"游戏管理器"和"悬浮窗倍率控制"两个页面内容重复。
_dup_tasks = [t for t, groups in args.items()
              if t != 'ModHandler' and 'ModHandler' in groups]
check('ModHandler 参数组没有残留在别的任务下', not _dup_tasks,
      f'残留于={_dup_tasks}')
eq('GameManager 任务下只剩 GameManager/Storage',
   sorted(args.get('GameManager', {}).keys()), ['GameManager', 'Storage'])

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
check('ModPrefs 已无 restart_policy（重启职责移交 ALAS 调度层）',
      not hasattr(_real_prefs, 'restart_policy'))

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

    # 严格单参签名：策略层只决定「开/关」，不再传 restart 之类的参数；
    # 若调用点残留旧参数（restart=True 等），这里会直接 TypeError。
    def fake_set_multiplier(mode):
        applied.append(mode)
        return True   # 与真实后端一致：写入成功返回 True

    handler.set_multiplier = fake_set_multiplier
    handler.read_backend_state = lambda: None
    handler._applied = applied
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

# 4.2b 策略层只传 mode：重启游戏不再由本功能决定（写完游戏保持关闭，
# ALAS 发现游戏没跑会自动排 Restart 任务）。fake 用严格单参签名，
# 调用点若残留 restart=True 之类的旧参数会直接 TypeError，本节就会失败。
h, cfg, dev = new_handler()
h.check_then_set('exercise')
eq('敏感任务关倍率：单参调用、动作一次', h._applied, [False])
h.check_then_set('main')
eq('常规任务开倍率：单参调用、动作一次', h._applied, [False, True])
h.check_then_set('event_a')
eq('状态已开时不动作', h._applied, [False, True])

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

# 4.10 启动检查只告警、不写设备（动作交给随后的任务决策）
h, cfg, dev = new_handler()
h.set_state(False)                       # 上次决定：关
h.read_backend_state = lambda: True      # 用户手动开回来了
changed = h.check_on_startup()
check('启动检查发现外部改动只告警、不写设备', changed is False and h._applied == [],
      f'changed={changed} applied={h._applied}')
# 紧接着的敏感任务按策略把倍率关掉（真正的动作只发生一次）
h.check_then_set('exercise')
check('外部开启被随后的敏感任务关掉', h._applied == [False], f'applied={h._applied}')

h, cfg, dev = new_handler()
h.set_state(True)
h.read_backend_state = lambda: True
h._applied.clear()
changed = h.check_on_startup()
check('启动检查：状态一致时不动作', changed is False and h._applied == [])

h, cfg, dev = new_handler()
h.set_state(False)
h.read_backend_state = lambda: None      # 读不到设备
h._applied.clear()
changed = h.check_on_startup()
check('启动检查：读不到设备时不动', changed is False and h._applied == [])

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
h.set_multiplier = lambda mode: False      # 后端没写成
h.read_backend_state = lambda: None
changed = h.check_then_set('exercise')
check('后端未改动时 check_then_set 返回 False', changed is False)

# 4.15b 敏感任务关倍率失败、设备确认还开着 -> 停机推送（RequestHumanTakeover）
h, cfg, dev = new_handler()
h.set_multiplier = lambda mode: False
h.read_backend_state = lambda: True        # 设备上倍率确实还开着
try:
    h.check_then_set('exercise')
    check('敏感任务关失败时抛 RequestHumanTakeover', False, '没有抛异常')
except mh.RequestHumanTakeover as e:
    check('敏感任务关失败时抛 RequestHumanTakeover', 'exercise' in str(e), str(e))
check('停机前不落盘「已关」的决定（不谎报）', h.get_state() is not True)

# 设备读不到状态时保守处理：只上报失败，不武断停机
h, cfg, dev = new_handler()
h.set_multiplier = lambda mode: False
h.read_backend_state = lambda: None
changed = h.check_then_set('exercise')
check('设备状态读不到时不停机、返回 False', changed is False)

# 常规任务开倍率失败也不停机（开了没开成只是少收益，不是封号风险）
h, cfg, dev = new_handler()
h.set_state(False)
h.set_multiplier = lambda mode: False
h.read_backend_state = lambda: False
changed = h.check_then_set('main')
check('常规任务开失败不停机、返回 False', changed is False)

# 4.15c 用户语义：不区分任务类型。
# 只要「该关倍率时没关掉」且回读确认设备上倍率仍开着，就停机 ——
# 该关而没关本身就说明链路出了问题（后端坏了、坐标漂移、key 映射错），
# 带着「以为关了其实没关」的状态继续跑就是封号风险。
# 所以 minigame（GROUP_ALWAYS_OFF，策略同样要求关）与未列出任务
# （沿用上次决定 = 关）同样在列，不再按「敏感 / 非敏感」区分。
for _task, _label in (('exercise', '演习'), ('coalition', '共斗'),
                      ('minigame', '小游戏'), ('commission', '未列出任务')):
    h, cfg, dev = new_handler()
    h.set_state(False)                     # 缓存：上次决定是关
    h.set_multiplier = lambda mode: False
    h.read_backend_state = lambda: True    # 设备上倍率确实还开着
    try:
        h.check_then_set(_task)
        check(f'{_label}({_task}) 该关没关掉时停机', False, '没有抛异常')
    except mh.RequestHumanTakeover as e:
        check(f'{_label}({_task}) 该关没关掉时停机', _task in str(e), str(e))

# 回读确认不了时（游戏没跑 / overlay 被崩溃保护停用）不武断停机：
# 后端返回 False 也可能只是「压根没能操作」，设备上未必真开着。
for _state, _label in ((None, '读不到状态'), (False, '确认已关')):
    h, cfg, dev = new_handler()
    h.set_state(False)
    h.set_multiplier = lambda mode: False
    h.read_backend_state = lambda s=_state: s
    try:
        changed = h.check_then_set('exercise')
        check(f'回读{_label}时不停机、返回 False', changed is False)
    except mh.RequestHumanTakeover:
        check(f'回读{_label}时不停机、返回 False', False, '不该停机')

# 4.15d Enabled=False 的强制关闭分支同样按「该关就停」处理
for _task in ('exercise', 'minigame'):
    h, cfg, dev = new_handler(Enabled=False)
    h.set_multiplier = lambda mode: False
    h.read_backend_state = lambda: True
    try:
        h.check_then_set(_task)
        check(f'Enabled=False 下 {_task} 关失败时停机', False, '没有抛异常')
    except mh.RequestHumanTakeover:
        check(f'Enabled=False 下 {_task} 关失败时停机', True)


# 4.14b ★ keys_configured 必须按后端区分（三个后端用的配置项完全不同）。
# ui 后端点的是 UiOffLabels / UiOnLabels，跟 OffKeys / OnKeys 无关；以前它也被要求
# 填 OffKeys，于是「Backend=ui + 填了 UiOffLabels + OffKeys 留空」会一路走到
# 「no keys configured, skipped」——用户看到的是功能完全不生效，很难联想到是这里。
h, cfg, dev = new_handler(Backend='ui', OffKeys='', OnKeys='', UiOffLabels='倍攻倍防')
check('Backend=ui + 配了 UiOffLabels -> keys_configured=True', h.keys_configured is True)
h, cfg, dev = new_handler(Backend='ui', OffKeys='', OnKeys='',
                          UiOffLabels='', UiOnLabels='倍攻倍防')
check('Backend=ui 只配 UiOnLabels 也算配了', h.keys_configured is True)
h, cfg, dev = new_handler(Backend='ui', OffKeys='', OnKeys='')
check('Backend=ui 什么都没配 -> keys_configured=False', h.keys_configured is False)
h, cfg, dev = new_handler(Backend='ui', OffKeys='1=1', OnKeys='1=1000')
check('Backend=ui 只填了 OffKeys/OnKeys（ui 用不上）-> 仍算没配',
      h.keys_configured is False)
h, cfg, dev = new_handler(Backend='prefs', OffKeys='', OnKeys='')
check('Backend=prefs 没配 OffKeys/OnKeys -> keys_configured=False',
      h.keys_configured is False)
h, cfg, dev = new_handler(Backend='overlay', OffKeys='', OnKeys='')
check('Backend=overlay 不需要 key -> keys_configured=True', h.keys_configured is True)

# 而且 ui 只配了标签时必须真的走到后端（不是停在「没配 key」那一步）
h, cfg, dev = new_handler(Backend='ui', OffKeys='', OnKeys='', UiOffLabels='倍攻倍防')
h.set_state(True)                       # 上次决定是开
h._applied.clear()
h.check_then_set('exercise')            # 演习要关 -> 应该真的调一次后端
eq('ui 后端只配标签时确实执行了关闭', h._applied, [False])

# 4.15d2 ★ 未确认达成时不能把「已达成」写进缓存。
# get_state() 的缓存会被 current_state() 当作「设备状态」的替身：一旦在
# 「后端没写成 + 回读读不到（None）」时把 multiplier_on=False 落盘，下一个敏感
# 任务就会读到 current == want_on 而直接 return —— 连写入都不再尝试，
# 正是「以为关了其实没关」。所以只有回读确认已达目标时才更新缓存。
h, cfg, dev = new_handler()
h.set_state(True)                        # 缓存：上次决定是开
h.set_multiplier = lambda mode: False    # 后端没写成
h.read_backend_state = lambda: None      # 而且回读读不到设备状态
eq('未确认达成时 check_then_set 返回 False', h.check_then_set('exercise'), False)
eq('未确认达成时不把「已关」写进缓存（否则下次直接跳过写入）', h.get_state(), True)

# 反过来：回读确认设备确实已经在目标状态时，缓存照旧要更新。
# 注意必须让两次回读给出不同结果：决策阶段读到 None（否则 current == want_on
# 会在入口就 return，根本走不到这里），复核阶段才读到 False。
h, cfg, dev = new_handler()
h.set_state(True)
h.set_multiplier = lambda mode: False
_reads = iter([None, False])             # 决策时读不到，复核时确认已关
h.read_backend_state = lambda: next(_reads, False)
eq('后端没动但设备确认已达目标 -> 仍返回 False', h.check_then_set('exercise'), False)
eq('设备确认已达目标时缓存更新为关', h.get_state(), False)

# 4.15d3 后果断言：一次「读不到状态」的失败之后，下一个敏感任务必须仍然去尝试写入。
# 这条正是 4.15d2 要防的场景 —— 缓存被污染成「已关」的话，第二次会一次后端调用都没有。
h, cfg, dev = new_handler()
h.set_state(True)
h.read_backend_state = lambda: None
_calls = []


def _always_fail(mode):
    _calls.append(mode)
    return False


h.set_multiplier = _always_fail
h.check_then_set('exercise')
h.check_then_set('exercise')
eq('读不到状态导致的失败不会让下一次敏感任务跳过写入', _calls, [False, False])

# 4.15e 后端抛异常必须落到「关失败」判定上。
# 以前异常直接冒泡到 alas.py 的宽 except，只记一条 warning 就继续跑任务 ——
# 于是「该关倍率时后端崩了」会带着倍率一路跑下去。
class _BoomBackend:
    def set_multiplier(self, mode):
        raise RuntimeError('overlay exploded')


h, cfg, dev = new_handler()
h._backend_obj = _BoomBackend()
del h.set_multiplier                    # 恢复真实的 ModHandler.set_multiplier
h.read_backend_state = lambda: True
try:
    h.check_then_set('exercise')
    check('后端抛异常时同样停机', False, '没有抛异常')
except mh.RequestHumanTakeover:
    check('后端抛异常时同样停机', True)


# 4.15f 后端主动抛 RequestHumanTakeover 时必须原样放行，
# 不能被 set_multiplier 的 except Exception 降级成「本次没改动」
class _TakeoverBackend:
    def set_multiplier(self, mode):
        raise mh.RequestHumanTakeover('backend wants human')


h, cfg, dev = new_handler()
h._backend_obj = _TakeoverBackend()
del h.set_multiplier
h.read_backend_state = lambda: None
try:
    h.check_then_set('main')
    check('后端抛 RequestHumanTakeover 时原样放行', False, '没有抛异常')
except mh.RequestHumanTakeover as e:
    check('后端抛 RequestHumanTakeover 时原样放行',
          'backend wants human' in str(e), str(e))

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
check('钩子放行 RequestHumanTakeover（该关倍率时关失败必须停机，不能被宽 except 吞掉）',
      'except RequestHumanTakeover' in alas_src)
# RequestHumanTakeover 从 loop() 冒出去只会被 process_manager 的
# `except Exception` 记一条 logger.exception，用户收不到任何消息、GUI 也只会
# 显示「跑完了」。所以停机必须在钩子里自己推送 + exit(1)。
_hook_src = alas_src.split('mod.check_then_set', 1)[-1].split('# Run', 1)[0]
check('停机前推送通知（Error_OnePushConfig）',
      'handle_notify' in _hook_src and 'Error_OnePushConfig' in _hook_src)
check('停机走 exit(1)，与「任务连续失败 3 次」同样的收尾',
      'exit(1)' in _hook_src)
check('RequestHumanTakeover 分支排在宽 except Exception 之前',
      _hook_src.index('except RequestHumanTakeover') < _hook_src.index('except Exception'))

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
