"""
ModHandler — 改版客户端（JMBQ / azurlan）悬浮窗倍率控制。

需求：悬浮窗的「倍率等影响舰船属性的功能」在敏感任务（META / 演习 / 共斗）时必须关闭，
其他任务正常启用。

设计：
  - 本模块只负责「策略」：判断当前任务该开还是该关，并驱动后端执行。
  - 后端（怎么改）与策略（什么时候改）分离：
      * ModPrefs  —— root 直接改模组的 SharedPreferences（MuMu 等已 root 环境，最稳）
      * ModUi     —— uiautomator2 点击悬浮窗开关（无 root 时的兜底）
  - 真实状态优先：每次决策先向后端读回设备上的实际开关状态（ModPrefs 读 XML，
    ModUi 展开悬浮窗读控件），读回失败才退回本地缓存。
    这样用户手动动过悬浮窗也能被纠正，而不是盲信 last-known。
  - 状态落盘到 config/mod_handler/mod_state_<config_name>.json，跨轮记忆。
  - 未列入任何分组的任务沿用「上次已做出的决定」，不做新的推测，
    避免未知任务把敏感任务的关闭状态顶掉（例如共斗之后紧跟 daily）。

敏感任务分组与 AlasGG 的 GGHandler.check_then_set_gg_status 保持一致，便于对照。
"""
import json
import os

from module.base.base import ModuleBase
from module.config.deep import deep_get
from module.logger import logger

# ---------------------------------------------------------------- 任务分组
# 演习
GROUP_EXERCISE = [
    'exercise',
]
# META / 余烬信标
GROUP_META = [
    'opsi_ash_assist',
    'opsi_ash_beacon',
]
# 共斗（大型作战共斗 + 日常共斗 + 怪谈纪实）
GROUP_RAID = [
    'raid',
    'raid_daily',
    'coalition',
    'coalition_sp',
]
# 大舰队（非敏感，可单独选择是否一并关闭；默认按「其他任务正常启用」处理）
GROUP_GUILD = [
    'guild',
]
# 明确允许开启倍率的常规任务（会进战斗、且不属于敏感项的）
# 说明：按当前 ALAS 版本实际存在的任务名核对过（dev_tools/mod_handler_selftest.py 会全量比对）；
#       未列入的任务不会被主动改动，避免对后勤任务做无谓的写配置+重启游戏。
GROUP_NORMAL = [
    # 主线
    'main',
    'main2',
    'main3',
    # 活动图
    'event',
    'event2',
    'event_a',
    'event_b',
    'event_c',
    'event_d',
    'event_sp',
    # 常规战斗
    'hard',
    'daily',
    'war_archives',
    'gems_farming',
    # 大舰队（大舰队作战会进战斗，默认正常启用；选「敏感项+大舰队」时会被覆盖为关闭）
    'guild',
    # 大型作战
    'opsi_explore',
    'opsi_daily',
    'opsi_obscure',
    'opsi_month_boss',
    'opsi_abyssal',
    'opsi_archive',
    'opsi_stronghold',
    'opsi_meowfficer_farming',
    'opsi_hazard1_leveling',
    'opsi_cross_month',
    # 旧版本曾有 / 其他分支存在，保留兼容（当前版本不存在时不会命中，无副作用）
    'sos',
    'maritime_escort',
    'event3',
    'c72_mystery_farming',
    'c122_medium_leveling',
    'c124_large_leveling',
]

# 无论如何都关闭倍率的任务（小游戏里倍率无意义且会干扰）
GROUP_ALWAYS_OFF = [
    'minigame',
]

# 与倍率无关的任务：通用/调度类、纯后勤类。
# 这些任务既不该开也不该关，直接跳过，避免多一次「写配置 + 重启游戏」。
# 注意：这里只放「确定不改变倍率」的名字，会进战斗的任务一律走 GROUP_NORMAL，
#       两个列表都不在的名字才会落到 NO_CHANGE_TASKS 之外的保守回退分支。
NO_CHANGE_TASKS = [
    # 调度 / 通用
    'restart',
    'general',
    'event_general',
    'opsi_general',
    'alas',
    'game_manager',
    # 后勤 / 非战斗
    'commission',
    'tactical',
    'research',
    'dorm',
    'meowfficer',
    'reward',
    'awaken',
    'shop_frequent',
    'shop_once',
    'shipyard',
    'gacha',
    'freebies',
    'private_quarters',
    'opsi_shop',
    'opsi_voucher',
    'opsi_daemon',
    'event_shop',
    'event_story',
    'island_production',
    'island_order',
    'island_freebie',
    'island_collect',
    'island_season_task',
    'island_business',
    'daemon',
    'island_production_planner',
    'benchmark',
    'azur_lane_uncensored',
    # 深海来信：剧情 + 单次 boss，战斗与倍率无关，避免多一次重启
    'hospital',
]

STATE_DIR = './config/mod_handler'


def resolve_policy(option):
    """
    把配置里的策略名解析为 (关闭列表, 开启列表)。

    与 AlasGG 的 DisabledTask 选项对齐：
      disable_all_dangerous_task   仅关敏感项（演习 + META + 共斗）  ← 默认，即需求所要求的行为
      disable_guild_and_dangerous  敏感项 + 大舰队
      disable_meta_and_exercise    仅关 META + 演习
      disable_exercise             仅关演习
      enable_all                   百无禁忌（不关任何任务）
    """
    disabled = list(GROUP_ALWAYS_OFF)
    enabled = list(GROUP_NORMAL)

    if option == 'disable_meta_and_exercise':
        disabled += GROUP_EXERCISE + GROUP_META
    elif option == 'disable_exercise':
        disabled += GROUP_EXERCISE
    elif option == 'enable_all':
        enabled += GROUP_RAID + GROUP_META + GROUP_EXERCISE
    elif option == 'disable_guild_and_dangerous':
        disabled += GROUP_EXERCISE + GROUP_META + GROUP_RAID + GROUP_GUILD
        # 大舰队改为关闭
        enabled = [t for t in enabled if t not in GROUP_GUILD]
    else:
        # 默认 disable_all_dangerous_task：仅关敏感项（演习 + META + 共斗）
        disabled += GROUP_EXERCISE + GROUP_META + GROUP_RAID

    return disabled, enabled


def sensitive_tasks(option=None):
    """
    返回该策略下「必须关闭倍率」的任务名（不含 minigame 这类无关项）。
    仅用于日志与自检展示。
    """
    disabled, _ = resolve_policy(option or 'disable_all_dangerous_task')
    return [t for t in disabled if t not in GROUP_ALWAYS_OFF]


def parse_key_values(text):
    """
    解析 "1=1,2=1,3=1" 形式的键值对，返回 {str: int} 。
    值支持 int / float / true / false / 字符串。
    """
    result = {}
    if not text:
        return result
    text = str(text).strip()
    if not text:
        return result
    # 兼容 JSON 写法
    if text.startswith('{'):
        try:
            data = json.loads(text)
            return {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.warning(f'ModHandler: invalid JSON key map {text!r}: {e}')
            return {}
    for pair in text.replace(';', ',').split(','):
        pair = pair.strip()
        if not pair or '=' not in pair:
            continue
        k, v = pair.split('=', 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if v.lower() in ('true', 'false'):
            result[k] = v.lower() == 'true'
            continue
        try:
            result[k] = int(v)
            continue
        except ValueError:
            pass
        try:
            result[k] = float(v)
            continue
        except ValueError:
            pass
        result[k] = v
    return result


class ModHandler(ModuleBase):
    """
    悬浮窗倍率策略控制器。

    Args:
        config: AzurLaneConfig
        device: Device；传 'skip' 表示只做配置级诊断、完全不碰设备
                （dev_tools 自检用，避免构造 Device 触发模拟器探测）。
    """

    # 同一进程内只提示一次「没配 OffKeys/OnKeys」，避免刷屏
    _keys_notice_shown = False

    def __init__(self, config=None, device=None):
        if device == 'skip':
            # 绑定设备对象会去连模拟器，诊断配置时并不需要
            device = None
            self._device_free = True
        else:
            self._device_free = False
        super().__init__(config=config, device=device)
        self.config = config
        self.device = device
        self._backend_obj = None
        # 本次会话里最近一次「明确做出」的决定（与磁盘缓存互为补充）
        self._last_want = None

    # ------------------------------------------------------------ 配置读取
    @property
    def enabled(self):
        return bool(deep_get(self.config.data, 'ModHandler.Enabled', default=False))

    @property
    def backend(self):
        return str(deep_get(self.config.data, 'ModHandler.Backend', default='prefs') or 'prefs')

    @property
    def sensitive_option(self):
        return str(deep_get(self.config.data,
                            'ModHandler.SensitiveTask',
                            default='disable_all_dangerous_task'))

    @property
    def off_keys(self):
        return parse_key_values(deep_get(self.config.data, 'ModHandler.OffKeys', default=''))

    @property
    def on_keys(self):
        return parse_key_values(deep_get(self.config.data, 'ModHandler.OnKeys', default=''))

    @property
    def keys_configured(self):
        """OffKeys / OnKeys 是否至少配了一个，没配时本功能只会打日志。"""
        return bool(self.off_keys or self.on_keys)

    # ------------------------------------------------------------ 状态落盘
    @property
    def _state_file(self):
        name = getattr(self.config, 'config_name', 'alas')
        return os.path.join(STATE_DIR, f'mod_state_{name}.json')

    def _read_state_file(self):
        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get_state(self):
        """
        读取本地缓存的倍率状态，未知时返回 None。
        Returns:
            True = 倍率开 / False = 倍率关 / None = 未知
        """
        value = self._read_state_file().get('multiplier_on')
        return value if isinstance(value, bool) else None

    def get_decided(self):
        """
        本地缓存是否来自一次「明确的决定」。
        未决定过时，启动阶段不应该强制纠偏（可能是用户自己开着的倍率）。
        """
        return bool(self._read_state_file().get('decided', False))

    def set_state(self, value, decided=True):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump({'multiplier_on': bool(value), 'decided': bool(decided)}, f)
        except Exception as e:
            logger.warning(f'ModHandler: failed to persist state: {e}')

    # ------------------------------------------------------------ 后端
    @property
    def _backend(self):
        """按配置实例化后端，复用实例避免重复连接设备。"""
        if self._backend_obj is None:
            if self.backend == 'ui':
                from module.mod_handler.mod_ui import ModUi
                self._backend_obj = ModUi(config=self.config, device=self.device)
            else:
                from module.mod_handler.mod_prefs import ModPrefs
                self._backend_obj = ModPrefs(config=self.config, device=self.device)
        return self._backend_obj

    def read_backend_state(self):
        """
        从设备上读取真实状态；后端不支持或读取失败时返回 None。
        这样用户手动动过悬浮窗也能被发现，而不是盲信本地缓存。
        """
        try:
            return self._backend.get_state()
        except Exception as e:
            logger.warning(f'ModHandler: read_backend_state failed: {e}')
            return None

    def current_state(self):
        """
        综合判断当前倍率状态：设备真实状态优先，读不到再退回本地缓存。
        """
        state = self.read_backend_state()
        if state is not None:
            return state, 'device'
        state = self.get_state()
        if state is not None:
            return state, 'cache'
        if self._last_want is not None:
            return self._last_want, 'session'
        return None, 'unknown'

    def set_multiplier(self, mode: bool):
        """
        按配置的后端切换倍率。
        Returns:
            bool: 后端是否真的做了改动（没配 key、或已是目标状态时为 False）
        """
        result = self._backend.set_multiplier(mode)
        if result:
            logger.attr('ModHandler', f'multiplier {"ON" if mode else "OFF"}')
        return bool(result)

    # ------------------------------------------------------------ 主逻辑
    def check_then_set(self, task, force=False):
        """
        任务开始前调用：按策略决定倍率开关，需要时驱动后端执行。

        Args:
            task: str，下划线形式的任务名，如 'exercise' / 'opsi_ash_beacon' / 'coalition'
            force: bool，忽略状态缓存，强制按策略执行一次
        Returns:
            bool: 本次是否真的改动了设备上的倍率
        """
        disabled, enabled = resolve_policy(self.sensitive_option)

        # 分组的优先级：显式关闭 > 显式开启
        if task in disabled:
            want_on = False
        elif task in enabled:
            want_on = True
        else:
            want_on = None

        if not self.enabled:
            # 功能关闭时也不是完全撒手：倍率在敏感任务里等同于封号风险，
            # 只要我们能读到设备状态且它正开着，就仍然关掉。
            if want_on is False and self.read_backend_state() is True:
                logger.hr('ModHandler', level=1)
                logger.warning('ModHandler 已关闭，但检测到敏感任务里倍率仍开着，强制关闭')
                changed = self.set_multiplier(False)
                self.set_state(False)
                return changed
            return False

        if want_on is None:
            # 不在任何分组里：不做新的推测。
            # 沿用「上一次明确的决定」，这样共斗 -> daily 之类不会把关闭状态顶掉；
            # 若本次会话与本地缓存都没有决定过，则完全不动。
            want_on = self._last_want
            if want_on is None:
                want_on = self.get_state()
            if want_on is None:
                logger.info(f'ModHandler: task `{task}` not in any group and no previous decision, '
                            f'leave multiplier as is')
                return False
            logger.info(f'ModHandler: task `{task}` not in any group, '
                        f'keep multiplier {"ON" if want_on else "OFF"}')

        current, source = self.current_state()
        if current is not None and current == want_on and not force:
            self._last_want = want_on
            logger.attr('ModHandler', f'{task}: multiplier already {"ON" if want_on else "OFF"}')
            return False

        # 没配 key 时后端无从下手。这里只跳过「写设备」，仍然记住本次决定，
        # 这样用户补上 key 之后策略立刻接得上，不会被中间任务弄乱。
        if not self.keys_configured:
            if not ModHandler._keys_notice_shown:
                ModHandler._keys_notice_shown = True
                logger.warning(
                    'ModHandler: ModHandler.OffKeys / OnKeys 均为空，无法改动悬浮窗开关。'
                    '请先运行 dev_tools/mod_discover.py 发现 key 映射，'
                    '或运行 dev_tools/mod_handler_doctor.py 自检当前环境。')
            self._last_want = want_on
            self.set_state(want_on)
            logger.info(f'Task `{task}` -> multiplier should be {"ON" if want_on else "OFF"} '
                        f'(no keys configured, skipped)')
            return False

        logger.hr('ModHandler', level=1)
        logger.info(f'Task `{task}` -> multiplier should be {"ON" if want_on else "OFF"} '
                    f'(current: {"unknown" if current is None else ("ON" if current else "OFF")}'
                    f'/{source})')
        changed = self.set_multiplier(want_on)
        if not changed:
            logger.warning(f'ModHandler: 后端没有改动任何开关（task `{task}`，'
                           f'target {"ON" if want_on else "OFF"}）')
        self._last_want = want_on
        self.set_state(want_on)
        return changed

    def check_on_startup(self):
        """
        调度器启动时的安全兜底。

        场景：上一次 ALAS 退出时停在敏感任务（倍率已关），用户手动把倍率开回去，
        而这次的第一个任务恰好是非敏感任务 —— 那会一直开着倍率跑到下一个敏感任务。
        这里读回设备真实状态，只要与「上次明确的决定」不一致就立刻纠偏一次。
        真正的开关策略仍由随后的 check_then_set 按任务决定。

        Returns:
            bool: 本次是否对倍率做了变更
        """
        if not self.enabled:
            return False
        if not self.get_decided():
            # 从未做出过决定，说明是首次启用/新配置，交给正常流程决定
            return False

        want_on = bool(self.get_state())
        if self._last_want is None:
            self._last_want = want_on

        current = self.read_backend_state()
        if current is None:
            logger.info('ModHandler: startup check skipped, cannot read device state')
            return False
        if current == want_on:
            logger.attr('ModHandler', f'startup check: multiplier already {"ON" if want_on else "OFF"}')
            return False

        wanted = f'{"ON" if want_on else "OFF"}'
        logger.hr('ModHandler', level=1)
        logger.warning(f'ModHandler: 悬浮窗倍率被外部改动（当前 {"ON" if current else "OFF"}，'
                       f'上次决定 {wanted}），按上次决定纠偏')
        changed = self.set_multiplier(want_on)
        self.set_state(want_on)
        return changed

    # ------------------------------------------------------------ 自检
    def diagnose(self):
        """
        输出当前环境是否具备控制条件，便于用户排查。
        Returns:
            dict: 诊断结果
        """
        disabled, enabled = resolve_policy(self.sensitive_option)
        info = {
            'enabled': self.enabled,
            'backend': self.backend,
            'sensitive_task': self.sensitive_option,
            'keys_configured': self.keys_configured,
            'cache_state': self.get_state(),
            'cache_decided': self.get_decided(),
            'off_tasks': [t for t in disabled if t not in GROUP_ALWAYS_OFF],
            'on_tasks_count': len(enabled),
        }
        if self._device_free or self.device is None:
            info['device'] = 'skipped'
            return info
        try:
            info.update(self._backend.diagnose())
        except Exception as e:
            info['error'] = str(e)
        return info
