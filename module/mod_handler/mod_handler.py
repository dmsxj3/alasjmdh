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

★ 不移植 AlasGG 的 power_limit()（开战前 OCR 战力上限兜底）。
  它的前提是 GG 修改器真的改了舰船属性，所以开战前 OCR 到的战力能反映「修改还开着」。
  本项目的悬浮窗倍率「不改战力」：开着也是正常战力，OCR 永远读到正常值，
  于是这道兜底永远不会触发 —— 那比没有兜底更糟，因为它会让人以为还有第二道保险。
  所以兜底只落在「写入后回读」这一层，且每一层都已经有了：
    * ModPrefs.verify_applied        —— 写完回读 XML，不一致即判失败
    * ModOverlay 点击后的 _at_target —— 点完绕开缓存重读，复核不通过即判失败
    * ModHandler.read_backend_state  —— 关失败时二次回读，确认不了「已关」即停机推送
"""
import json
import os

from module.config.deep import deep_get
from module.exception import RequestHumanTakeover
from module.logger import logger


class _ModuleBaseStub:
    """PIL 不可用时的降级基类，见下面的 import 说明。"""

    def __init__(self, config=None, device=None, task=None):
        self.config = config
        self.device = device


try:
    # module/base/base.py 第 1 行 -> module.base.button -> `from PIL import ImageDraw`。
    # webui 进程把 PIL 换成了假模块（只有 PIL.Image.Image），这里会 ImportError。
    # 只读展示路径只需要下面这些纯函数，所以降级即可，不影响只读展示。
    from module.base.base import ModuleBase
except ImportError:  # pragma: no cover - 取决于运行环境
    ModuleBase = _ModuleBaseStub

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

# 无论如何都关闭倍率的任务。
# ★ 现为空列表：曾按 AlasGG 原版把 minigame（小游戏）放在这里，但倍率/战力
#   修改只影响战斗数值，小游戏读不到也用不上这些值，实测开/关倍率都能正常
#   跑 —— 为它每个任务边界开关一次纯属浪费时间，已移入 NO_CHANGE_TASKS。
GROUP_ALWAYS_OFF = []

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
    # 本功能自己的配置页（工具页里的一个任务，不参与战斗）
    'mod_handler',
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
    # 小游戏：不读倍率/战力数值，开不开都能跑，不做任何干预（省一次开关）
    'minigame',
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
    返回该策略下「必须关闭倍率」的任务名（演习 + META + 共斗）。
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
            # 'skip'：只做配置级诊断，绝不碰设备。
            # 不能走 ModuleBase.__init__ —— 它在 device=None 时会自动构造 Device
            # 去连模拟器（module/base/base.py 的固定行为），这正是 'skip' 要避免的；
            # 之前把 device 置 None 再调 super().__init__，Device 照样被建出来。
            # 这里手动补齐 ModuleBase 会初始化的属性
            # （early_ocr_import 对本类是 no-op：EARLY_OCR_IMPORT=False）。
            self._device_free = True
            self.config = config
            self.device = None
            self.interval_timer = {}
            self._backend_obj = None
            self._last_want = None
            return
        self._device_free = False
        super().__init__(config=config, device=device)
        self.config = config
        # 注意：device=None 时 ModuleBase 已经自动按 config 建好了 Device，
        # 这里不能无条件覆盖，否则 self.device 会变成 None，
        # 之后所有 adb 操作都会失败（ModPrefs: 'NoneType' has no attribute 'adb_shell'）。
        if device is not None:
            self.device = device
        self._backend_obj = None
        # 本次会话里最近一次「明确做出」的决定（与磁盘缓存互为补充）
        self._last_want = None

    # ------------------------------------------------------------ 配置读取
    # 注意路径是 ModHandler.ModHandler.<参数>：ALAS 的任务名与参数组名同名时，
    # config.data 里就是 <任务>.<组>.<参数> 两层。写成 ModHandler.Enabled 会
    # 悄悄读不到（deep_get 返回 default），功能看着"没反应"。
    @property
    def enabled(self):
        return bool(deep_get(self.config.data, 'ModHandler.ModHandler.Enabled', default=False))

    @property
    def backend(self):
        return str(deep_get(self.config.data, 'ModHandler.ModHandler.Backend',
                            default='overlay') or 'overlay')

    @property
    def sensitive_option(self):
        return str(deep_get(self.config.data,
                            'ModHandler.ModHandler.SensitiveTask',
                            default='disable_all_dangerous_task'))

    @property
    def off_keys(self):
        return parse_key_values(
            deep_get(self.config.data, 'ModHandler.ModHandler.OffKeys', default=''))

    @property
    def on_keys(self):
        return parse_key_values(
            deep_get(self.config.data, 'ModHandler.ModHandler.OnKeys', default=''))

    @property
    def ui_labels(self):
        """ui 后端的开关文本（UiOffLabels + UiOnLabels），仅用于「有没有配」的判断。"""
        labels = []
        for path in ('ModHandler.ModHandler.UiOffLabels', 'ModHandler.ModHandler.UiOnLabels'):
            for item in str(deep_get(self.config.data, path, default='') or '').replace(
                    '；', ',').replace(';', ',').split(','):
                if item.strip() and item.strip() not in labels:
                    labels.append(item.strip())
        return labels

    @property
    def keys_configured(self):
        """
        本后端有没有可用的「怎么改」配置；没配时本功能只打日志、不动设备。

        ★ 必须按后端区分，三个后端用的配置项完全不同：
          overlay —— 靠识图点滑块/开关，不需要 OffKeys / OnKeys；
          ui      —— 点的是 UiOffLabels / UiOnLabels，与 OffKeys / OnKeys 无关；
          prefs   —— 才是真正写 OffKeys / OnKeys 的那个。
        以前只有 overlay 走了特例，ui 也去查 OffKeys：于是「选 ui + 填了
        UiOffLabels + OffKeys 留空」会一路走到「no keys configured, skipped」，
        用户看到的是功能完全不生效，很难联想到是这里判错了。
        """
        if self.backend == 'overlay':
            return True
        if self.backend == 'ui':
            return bool(self.ui_labels)
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
            if self.backend == 'overlay':
                from module.mod_handler.mod_overlay import ModOverlay
                self._backend_obj = ModOverlay(config=self.config, device=self.device)
            elif self.backend == 'ui':
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

        ★ 统一归一化成真 bool / None：识图路径（overlay / ui）容易带出 numpy 布尔，
        而下游是用 `is True` / `is False` 做安全判定的（_confirm_off_or_stop、
        check_then_set 的缓存写入），np.bool_(True) 会让那些判定静默失效。
        """
        try:
            state = self._backend.get_state()
        except Exception as e:
            logger.warning(f'ModHandler: read_backend_state failed: {e}')
            return None
        return None if state is None else bool(state)

    def describe_state(self):
        """
        给 GUI 展示用的「当前真实倍率状态」，直读设备，不看本地缓存。

        Returns:
            dict: {'option': 'on'/'off'/'unknown'/'unconfigured', 'detail': str}
        """
        if self._device_free or self.device is None:
            return {'option': 'unknown', 'detail': '未绑定设备（仅配置级诊断）'}
        try:
            return self._backend.describe_state()
        except Exception as e:
            return {'option': 'unknown', 'detail': f'读取失败: {type(e).__name__}: {e}'}

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

    def repair_partial(self, mode: bool):
        """
        设备只有部分键落在目标值上时，只补写缺失的那些。

        后端不支持（ui 没有 repair）或没有可补的键时返回 False，调用方走完整流程。

        Returns:
            bool: 是否真的补写了
        """
        try:
            result = self._backend.repair(mode)
        except AttributeError:
            return False
        except Exception as e:
            logger.warning(f'ModHandler: repair_partial failed: {e}')
            return False
        if result:
            logger.attr('ModHandler', f'repaired to {"ON" if mode else "OFF"}')
        return bool(result)

    def set_multiplier(self, mode: bool):
        """
        按配置的后端切换倍率。
        overlay 后端（默认）点悬浮窗开关直接写入，立即生效；
        prefs 后端写 XML，需要停游戏，写完游戏保持关闭、由 ALAS 拉起。

        Args:
            mode: True = 开倍率，False = 关倍率
        Returns:
            bool: 后端是否确认达成目标状态（没配 key、或回读校验未通过时为 False）
        """
        try:
            result = self._backend.set_multiplier(mode)
        except RequestHumanTakeover:
            # 后端主动要求停机（例如 prefs 发现 Error.HandleError 关闭后无法停游戏）：
            # 必须放行，不能被下面的 except Exception 降级成「本次没改动」。
            raise
        except Exception as e:
            # 后端抛异常 = 本次切换根本没做到。以前让它冒泡到调度循环，被
            # alas.py 的宽 except 记一条 warning 就继续跑任务了 —— 于是「该关
            # 倍率时后端崩了」会带着倍率一路跑下去。现在统一降级成 False，
            # 由调用方走「关失败」的判定（含自动降级与日志），异常栈记在这里便于定位。
            logger.exception(f'ModHandler: 后端切换倍率异常: {type(e).__name__}: {e}')
            return False
        if result:
            logger.attr('ModHandler', f'multiplier {"ON" if mode else "OFF"}')
        return bool(result)

    def _confirm_off_or_stop(self, task):
        """
        该关倍率时没关掉 —— 后端内部已自动降级过一次（停游戏 + prefs 重写，
        见 ModOverlay.set_multiplier）仍没达成。这里回读设备做最终裁决：
        只有回读确认「已关」才放行，否则停机推送交人工。

        ★ 为什么 None（读不到）也停：降级失败意味着 prefs 通道断了（root 断、
        读不到/写不进 XML），游戏重新拉起后倍率状态未知 —— 大概率仍是开的
        （XML 里留着什么就是什么，例如用户手动开过）。此时继续跑敏感任务就是
        封号风险，而且下一次重试大概率还是失败，只会一直带着倍率跑下去。
        停机推送（alas.py 统一 Onepush + exit）是唯一安全的收尾。

        Returns:
            bool: False = 回读确认设备已关（安全，调用方继续正常流程）。
                  其余情况（True = 确认仍开着 / None = 读不到）抛
                  RequestHumanTakeover，不返回。
        """
        observed = self.read_backend_state()
        if observed is False:
            return False
        logger.hr('ModHandler', level=1)
        if observed is True:
            logger.critical(
                f'ModHandler: 任务 `{task}` 需要关闭倍率，关闭失败且自动降级后回读确认'
                f'倍率仍开着 —— 继续跑下去就是封号风险，停止 Alas 交人工处理')
            raise RequestHumanTakeover(
                f'ModHandler: failed to turn multiplier off for task `{task}`')
        logger.critical(
            f'ModHandler: 任务 `{task}` 关闭倍率失败，自动降级后仍读不到倍率状态 —— '
            f'常见于手动改过倍率后 root/读取断开，游戏拉起后倍率大概率仍开着，'
            f'继续跑下去就是封号风险，停止 Alas 交人工处理')
        raise RequestHumanTakeover(
            f'ModHandler: multiplier state unreadable for task `{task}` after auto-fallback')

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
                logger.warning('ModHandler 已关闭，但检测到该关倍率的任务里倍率仍开着，强制关闭')
                changed = self.set_multiplier(False)
                if not changed:
                    # 后端内部已自动降级仍没达成：回读确认「已关」才继续，
                    # 否则（确认还开着 / 读不到）停机推送。
                    self._confirm_off_or_stop(task)
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
            if task in disabled and source != 'device':
                # ★ 敏感任务的 already 判定必须以设备实时读数为准（2026-09-12 现场教训）。
                # 用户手动把倍率拨到自定义档位（既不在 OffKeys 也不在 OnKeys 上）时，
                # 设备读数只能是 None，current_state() 会退回本地缓存 —— 旧逻辑拿着
                # 缓存的 OFF 直接放行，敏感任务带着倍率开打。这里不认缓存短路，
                # 强制走一次写入流程，让后端以设备真实通道重新确认/写入；
                # 若写入也失败且回读仍是 None，由失败分支的日志与缓存保留收尾（不停机）。
                # 常规任务维持缓存幂等：倍率开着对它们是正常态，不必每次都碰设备。
                logger.warning(
                    f'ModHandler: `{task}` 需要倍率 OFF，但设备真实状态读不到'
                    f'（缓存显示 OFF，可能是手动改过倍率或读取断开）—— '
                    f'不信任缓存，重新执行一次关闭')
            else:
                logger.attr('ModHandler', f'{task}: multiplier already {"ON" if want_on else "OFF"}')
                return False

        # 设备可能停在"部分匹配"的中间状态（用户手动拨过某个开关，例如
        # 倍率已关但以德服人还开着）。这时只补写缺的那几个键，
        # 不要把全部键重写一遍 —— 否则每个任务边界都要重启一次游戏。
        if not force:
            repaired = self.repair_partial(want_on)
            if repaired:
                self._last_want = want_on
                self.set_state(want_on)
                return True

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

        # 敏感任务 = 必须关闭倍率的任务（META / 演习 / 共斗）。
        # overlay 后端：调起悬浮窗点击开关，native 改内存立即生效，不重启游戏。
        sensitive = task in disabled

        if sensitive:
            logger.warning(f'敏感任务 `{task}`：关闭倍率（悬浮窗点击，立即生效）')
        else:
            logger.info(f'Task `{task}` -> multiplier should be '
                        f'{"ON" if want_on else "OFF"} '
                        f'(current: {"unknown" if current is None else ("ON" if current else "OFF")}'
                        f'/{source})')

        changed = self.set_multiplier(want_on)
        if not changed:
            # 后端返回 False 只剩「尝试过但没达成」（幂等已由后端返回 True 表达）。
            # 后端内部已经自动降级过一次（停游戏 + prefs 重写）仍没达成。
            if want_on is False:
                # 该关而没关掉：回读确认「已关」才继续，否则（确认还开着 /
                # 读不到）停机推送 —— 降级失败后游戏拉起时倍率大概率仍开着，
                # 绝不带着疑问开打敏感任务。
                observed = self._confirm_off_or_stop(task)
            else:
                observed = self.read_backend_state()
            self._last_want = want_on
            if observed is not want_on:
                # ★ 没达成目标时**不要**把目标写进缓存（want_on=True 的常规
                # 任务路径才走得到这里；OFF 路径的缓存卫生由
                # _confirm_off_or_stop 的「确认已关」前提保证）。
                logger.warning(
                    f'ModHandler: 未能确认达成目标（task `{task}`，'
                    f'target {"ON" if want_on else "OFF"}，回读 '
                    f'{"unknown" if observed is None else ("ON" if observed else "OFF")}），'
                    f'保留原有缓存，下次决策会重新尝试')
                return False
            logger.warning(f'ModHandler: 后端没有改动任何开关（task `{task}`，'
                           f'target {"ON" if want_on else "OFF"}）')
        self._last_want = want_on
        self.set_state(want_on)
        return changed

    def check_on_startup(self):
        """
        调度器启动时的安全检查（只检测、不写设备）。

        发现设备状态与「上次明确的决定」不一致时只告警；真正的动作交给
        随后的 check_then_set 按第一个任务的策略执行：
          - 第一个任务是敏感任务 -> 关掉（只写一次）
          - 第一个任务是常规任务 -> 开回来（只写一次）
          - 未列出的任务 -> 沿用上次决定，设备与决定不符时同样会被纠正
        旧版在这里直接按缓存纠偏写设备：缓存陈旧时会出现
        「纠偏开回来 -> 下一个敏感任务又关掉」的双写，还多停一次游戏；
        用户手动改过倍率时也会被旧缓存顶掉。

        Returns:
            bool: 恒为 False（本方法不再改动设备，返回值仅为兼容旧调用方）
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

        logger.hr('ModHandler', level=1)
        logger.warning(f'ModHandler: 悬浮窗倍率被外部改动（当前 {"ON" if current else "OFF"}，'
                       f'上次决定 {"ON" if want_on else "OFF"}），'
                       f'等待下一个任务的策略处理')
        return False

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
