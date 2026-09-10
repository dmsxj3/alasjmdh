"""
mod_handler 测试工具包 —— 让 mod_handler 三个模块可以在「没有 emulator、没有游戏」的
前提下被完整测试。

为什么需要它：
  module/mod_handler/* 正常导入时会拉起 module.base.base -> module.device.device，
  构造 Device 会真的去连模拟器。测试只需要策略与编排逻辑，所以这里把
  module.base.base / module.logger / module.exception 换成桩，再导入真实模块。

对外提供：
  install_stubs()      注入桩模块（幂等）
  FakeConfig           带 .data 的假配置
  FakeDevice           记录 app_start/app_stop/adb_shell 的假设备
  load_real_tasks()    从 module/config/argument/task.yaml 读真实任务名
  Checker              断言与汇总
"""
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_installed = False


# ---------------------------------------------------------------- 桩模块
class _FakeLogger:
    """静默 logger：测试要看的只有断言结果。"""

    def __init__(self):
        self.records = []

    def _log(self, level, msg):
        self.records.append((level, str(msg)))

    def info(self, msg='', *args, **kwargs):
        self._log('info', msg)

    def warning(self, msg='', *args, **kwargs):
        self._log('warning', msg)

    def error(self, msg='', *args, **kwargs):
        self._log('error', msg)

    def critical(self, msg='', *args, **kwargs):
        self._log('critical', msg)

    def hr(self, msg='', level=1):
        self._log('hr', msg)

    def attr(self, name, value):
        self._log('attr', f'{name}={value}')

    def exception(self, msg='', *args, **kwargs):
        self._log('exception', msg)

    def set_file_logger(self, *args, **kwargs):
        pass


fake_logger = _FakeLogger()


class RequestHumanTakeover(Exception):
    """与 module.exception.RequestHumanTakeover 同名同义。"""


def install_stubs():
    """把重依赖换成桩，之后就能安全 import module.mod_handler.*"""
    global _installed
    if _installed:
        return
    sys.path.insert(0, ROOT)

    def put(name, module):
        sys.modules[name] = module

    # module.base.base
    base_mod = types.ModuleType('module.base.base')

    class ModuleBase:
        def __init__(self, config=None, device=None, task=None):
            self.config = config
            self.device = device

        @property
        def app_stop(self):
            raise AssertionError('ModuleBase stub has no app_stop')

    base_mod.ModuleBase = ModuleBase
    put('module.base.base', base_mod)

    # module.logger
    logger_mod = types.ModuleType('module.logger')
    logger_mod.logger = fake_logger
    put('module.logger', logger_mod)

    # module.exception
    exc_mod = types.ModuleType('module.exception')
    exc_mod.RequestHumanTakeover = RequestHumanTakeover
    exc_mod.ScriptError = Exception
    put('module.exception', exc_mod)

    _installed = True


# ---------------------------------------------------------------- 假对象
class FakeConfig:
    """最小可用的假配置：ModHandler 只用到 .data 与 .config_name。"""

    def __init__(self, config_name='alas', **mod_values):
        values = {
            'Enabled': True,
            'Backend': 'prefs',
            'PackageName': 'com.bilibili.azurlane',
            'PrefsFile': 'com.bilibili.azurlane_preferences',
            'OffKeys': '1=1,2=1,3=1',
            'OnKeys': '1=1000,2=1000,3=1000',
            'RestartGame': True,
            'RestartTask': 'always',
            'SensitiveTask': 'disable_all_dangerous_task',
            'UiOpenPoint': '',
            'UiClosePoint': '',
            'UiOffLabels': '',
            'UiOnLabels': '',
            'UiTapPoints': '',
            'UiWaitTimeout': 5,
        }
        values.update(mod_values)
        self.config_name = config_name
        # 与真实 config.data 同构：任务名.参数组.参数（ModHandler 任务 / ModHandler 组）
        self.data = {'ModHandler': {'ModHandler': values, 'Storage': {'Storage': {}}}}

    def set(self, **kwargs):
        self.data['ModHandler']['ModHandler'].update(kwargs)
        return self


class FakeDevice:
    """记录 app_start / app_stop / adb_shell 的假设备。"""

    def __init__(self, package='com.bilibili.azurlane', root=True):
        self.serial = 'fake-serial'
        self.package = package
        self.root = root
        self.calls = []
        self.running = True
        self.raise_on_app_stop = None
        self.raise_on_app_start = None
        self.pushed = {}

    # --- 记录型接口 ---
    def app_stop(self):
        self.calls.append('app_stop')
        if self.raise_on_app_stop is not None:
            raise self.raise_on_app_stop
        self.running = False

    def app_start(self):
        self.calls.append('app_start')
        if self.raise_on_app_start is not None:
            raise self.raise_on_app_start
        self.running = True

    def adb_push(self, local, remote):
        self.calls.append(f'adb_push:{remote}')
        with open(local, 'rb') as f:
            self.pushed[remote] = f.read().decode('utf-8')

    def adb_shell(self, cmd, timeout=10, **kwargs):
        if isinstance(cmd, (list, tuple)):
            cmd = ' '.join(str(c) for c in cmd)
        cmd = str(cmd)
        self.calls.append(f'adb_shell:{cmd}')

        if cmd.startswith('su -c'):
            inner = cmd[len('su -c'):].strip()
            inner = inner.strip("'").replace("'\\''", "'")
            return self._su(inner)
        if cmd.startswith('pidof'):
            return f'1234\n' if self.running else ''
        return ''

    def _su(self, inner):
        if not self.root:
            return 'uid=1000(u0_a46) gid=1000(u0_a46)'
        if inner.strip() == 'id':
            return 'uid=0(root) gid=0(root)'
        return ''


# ---------------------------------------------------------------- 真实任务名
def _load_yaml(path):
    """用 ALAS 自带的 yaml 解析，保证与 ALAS 自身看到的定义完全一致。"""
    sys.path.insert(0, os.path.join(ROOT, 'toolkit', 'lib', 'site-packages'))
    import yaml
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_real_tasks():
    """
    从 module/config/argument/task.yaml 读取真实任务名（CamelCase），
    转成 ALAS 运行时用的下划线形式，与 scheduler 传入 check_then_set 的完全一致。

    Returns:
        list[str]: 全部任务名
    """
    import inflection
    path = os.path.join(ROOT, 'module', 'config', 'argument', 'task.yaml')
    data = _load_yaml(path)
    names = []
    for group in data.values():
        for task in group.get('tasks', {}) or {}:
            names.append(task)
    return [inflection.underscore(n) for n in names]


def load_argument(group):
    """
    读取 argument.yaml 里某个参数组的定义。
    Returns:
        dict: {key: 定义}，标题行与 _info 不在其中
    """
    path = os.path.join(ROOT, 'module', 'config', 'argument', 'argument.yaml')
    data = _load_yaml(path)
    return data.get(group, {})


def default_of(definition):
    """把参数定义取值；只有 value 的简写形式直接返回自身。"""
    if isinstance(definition, dict):
        return definition.get('value')
    return definition


# ---------------------------------------------------------------- 断言
class Checker:
    def __init__(self, title):
        self.title = title
        self.fail = 0
        self.passed = 0

    def header(self, text):
        print(f'\n===== {text} =====')

    def check(self, label, cond, extra=''):
        if cond:
            self.passed += 1
        else:
            self.fail += 1
        status = 'PASS' if cond else 'FAIL'
        print(f'[{status}] {label}' + (f'  {extra}' if extra else ''))
        return bool(cond)

    def eq(self, label, got, expect):
        return self.check(label, got == expect, f'got={got!r} expect={expect!r}')

    def summary(self):
        print(f'\n===== {self.title}: '
              f'{"全部通过" if self.fail == 0 else str(self.fail) + " 项失败"} '
              f'({self.passed} passed) =====')
        return self.fail
