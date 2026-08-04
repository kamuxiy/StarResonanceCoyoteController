"""内置 GameValueDetector 监控器 —— 替代外部 DGLabGameController 的场景评估逻辑。

将 DGLabGameController 的 GameValueDetector 模块核心逻辑内置到 Python 客户端：
1. 监控游戏状态字段（与共享内存相同的 10 个字段）
2. 评估场景（Increased/Decreased/Changed/GreaterThan 等）
3. 触发动作（Fire/SetStrength 等）通过 DGLabClient 发送到 DGLabGameController

这样无需启动外部 DGLabGameController 的 GameValueDetector 模块，
直接在客户端内部完成场景评估和动作触发，方便调试。
"""
import threading
import time
import logging

logger = logging.getLogger(__name__)

# ── 字段定义（与 shared_state.py 一致）──
FIELD_KEYS = [
    "any_player_dead", "dead_count", "player_count",
    "current_pulse", "next_bonus", "one_click_bonus",
    "trigger_count", "self_hp_percent", "self_current_hp", "self_max_hp",
]

FIELD_TYPES = {
    "any_player_dead": "Int32", "dead_count": "Int32", "player_count": "Int32",
    "current_pulse": "Int32", "next_bonus": "Int32", "one_click_bonus": "Int32",
    "trigger_count": "Int32", "self_hp_percent": "Float",
    "self_current_hp": "Int32", "self_max_hp": "Int32",
}

SCENARIO_TYPES = [
    "Changed", "Increased", "Decreased", "EqualTo",
    "GreaterThan", "LessThan", "NotEqualTo",
    "PercentLessThan", "PercentGreaterThan"
]

ACTION_TYPES = [
    "SetStrengthSet", "SetStrengthAdd", "SetStrengthSub",
    "SetRandomStrengthSet", "SetRandomStrengthAdd", "SetRandomStrengthSub",
    "Fire"
]

ACTION_MODES = [
    "default", "Fixed", "Diff", "MemoryValue",
    "Percent", "Reverse_Percent", "ChangePercent", "Reverse_ChangePercent"
]


class MonitorItem:
    """单个监控项：监控一个字段的场景规则。"""

    def __init__(self, field_key: str, scenario: str, compare_value: float,
                 action: str, action_mode: str, action_value: float,
                 time_ms: int, overrides: bool = False,
                 start_condition: str = "Always"):
        self.field_key = field_key
        self.scenario = scenario
        self.compare_value = compare_value
        self.action = action
        self.action_mode = action_mode
        self.action_value = action_value
        self.time_ms = time_ms
        self.overrides = overrides
        self.start_condition = start_condition
        # 运行时状态
        self._prev_value = None
        self._max_value = None
        self._fired = False  # 一次性触发标记

    def reset(self):
        """重置运行时状态。"""
        self._prev_value = None
        self._max_value = None
        self._fired = False

    def evaluate(self, current_value) -> bool:
        """评估场景是否触发。返回 True 表示触发。

        匹配反编译 GameValueDetector.dll 的逻辑：
        - GreaterThan 实际用 >=
        - LessThan 实际用 <=
        - Increased/Decreased/Changed 比较差值
        """
        if current_value is None:
            return False

        # StartCondition 检查
        if self.start_condition == "ValueNotZero" and current_value == 0:
            return False

        prev = self._prev_value
        triggered = False

        if self.scenario == "Changed":
            triggered = prev is not None and current_value != prev

        elif self.scenario == "Increased":
            triggered = prev is not None and current_value > prev

        elif self.scenario == "Decreased":
            triggered = prev is not None and current_value < prev

        elif self.scenario == "EqualTo":
            triggered = current_value == self.compare_value

        elif self.scenario == "GreaterThan":
            # C# 源码用 >= （反编译确认）
            triggered = current_value >= self.compare_value

        elif self.scenario == "LessThan":
            # C# 源码用 <= （反编译确认）
            triggered = current_value <= self.compare_value

        elif self.scenario == "NotEqualTo":
            triggered = current_value != self.compare_value

        elif self.scenario == "PercentLessThan":
            if prev is not None and prev != 0:
                pct = (current_value / prev) * 100
                triggered = pct < self.compare_value

        elif self.scenario == "PercentGreaterThan":
            if prev is not None and prev != 0:
                pct = (current_value / prev) * 100
                triggered = pct > self.compare_value

        # 更新最大值
        if self._max_value is None or current_value > self._max_value:
            self._max_value = current_value

        # 更新前值
        self._prev_value = current_value

        return triggered


class GameValueMonitor:
    """内置 GameValueDetector 监控器。

    管理所有监控项，接收状态更新，评估场景，触发动作。
    """

    def __init__(self, dglab_client=None):
        self._client = dglab_client
        self._monitors: list[MonitorItem] = []
        self._lock = threading.Lock()
        self._enabled = False
        self._values = {}  # 当前状态值
        self._trigger_log = []  # 触发日志 [(timestamp, monitor, value)]
        self._max_log = 200

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def monitors(self) -> list:
        return self._monitors

    @property
    def values(self) -> dict:
        return self._values.copy()

    @property
    def trigger_log(self) -> list:
        return self._trigger_log.copy()

    def set_client(self, client):
        """设置 DGLabClient 实例。"""
        self._client = client

    def start(self):
        """启动监控器。"""
        self._enabled = True
        for m in self._monitors:
            m.reset()

    def stop(self):
        """停止监控器。"""
        self._enabled = False

    def clear_monitors(self):
        """清空所有监控项。"""
        with self._lock:
            self._monitors.clear()

    def add_monitor(self, monitor: MonitorItem):
        """添加一个监控项。"""
        with self._lock:
            self._monitors.append(monitor)

    def add_monitor_from_dict(self, d: dict):
        """从字典添加监控项。"""
        m = MonitorItem(
            field_key=d.get("FieldKey", d.get("field_key", "")),
            scenario=d.get("Scenario", "Increased"),
            compare_value=float(d.get("CompareValue", 0)),
            action=d.get("Action", "Fire"),
            action_mode=d.get("ActionMode", "Default"),
            action_value=float(d.get("ActionValue", 1)),
            time_ms=int(d.get("Time", 3000)),
            overrides=bool(d.get("Overrides", False)),
            start_condition=d.get("StartCondition", "Always"),
        )
        self.add_monitor(m)

    def load_default_monitors(self):
        """加载默认监控配置（与 _auto_export_gvd_json 一致）。"""
        self.clear_monitors()

        # 监控项1：任意玩家阵亡 → Fire
        self.add_monitor(MonitorItem(
            "any_player_dead", "Increased", 0,
            "Fire", "Default", 1, 3000, True
        ))
        # 监控项2：阵亡人数增加 → Fire
        self.add_monitor(MonitorItem(
            "dead_count", "Increased", 0,
            "Fire", "Default", 1, 2000, True
        ))
        # 监控项3：血量下降 → 增加强度
        self.add_monitor(MonitorItem(
            "self_hp_percent", "Decreased", 0,
            "SetStrengthAdd", "ChangePercent", 1, 1000, False
        ))
        # 监控项4：血量回升 → 降低强度
        self.add_monitor(MonitorItem(
            "self_hp_percent", "Increased", 0,
            "SetStrengthSub", "Percent", 0.2, 1000, False
        ))
        # 监控项5：脉冲增加 → 微增强度
        self.add_monitor(MonitorItem(
            "current_pulse", "Increased", 0,
            "SetStrengthAdd", "Default", 0.1, 1000, False
        ))
        # 监控项6：脉冲减少 → 微减强度
        self.add_monitor(MonitorItem(
            "current_pulse", "Decreased", 0,
            "SetStrengthSub", "Default", 0.1, 1000, False
        ))

    def update_state(self, **kwargs):
        """接收状态更新，评估所有监控项。

        参数与 SharedStateWriter.update() 一致。
        """
        if not self._enabled:
            # 即使未启用也保存值（供调试页查看）
            self._values.update(kwargs)
            return

        with self._lock:
            # 更新状态值
            self._values.update(kwargs)

            # 评估每个监控项
            for m in self._monitors:
                field = m.field_key
                if field not in kwargs:
                    # 字段未更新，使用缓存的值
                    val = self._values.get(field)
                    if val is None:
                        continue
                else:
                    val = kwargs[field]

                try:
                    if m.evaluate(val):
                        self._execute_action(m, val)
                except Exception as e:
                    logger.warning(f"监控项评估失败 [{field}/{m.scenario}]: {e}")

    def _execute_action(self, monitor: MonitorItem, current_value):
        """执行动作（通过 DGLabClient 发送指令）。"""
        ts = time.strftime("%H:%M:%S")
        log_entry = (ts, f"{monitor.field_key}/{monitor.scenario}",
                     current_value, monitor.action)
        self._trigger_log.append(log_entry)
        if len(self._trigger_log) > self._max_log:
            self._trigger_log = self._trigger_log[-self._max_log:]

        action = monitor.action
        av = monitor.action_value
        t = monitor.time_ms

        if self._client is None:
            logger.debug(f"[Monitor] 触发但无 DGLabClient: {log_entry}")
            return

        try:
            if action == "Fire":
                strength = int(min(av * 20 if av <= 2 else av, 40))
                self._client.fire_async(strength=strength, time_ms=t,
                                        override=monitor.overrides)
            elif action == "SetStrengthSet":
                self._client.set_strength_async(set=int(av))
            elif action == "SetStrengthAdd":
                self._client.set_strength_async(add=int(av))
            elif action == "SetStrengthSub":
                self._client.set_strength_async(sub=int(av))
            elif action == "SetRandomStrengthSet":
                self._client.set_random_strength(set=int(av))
            elif action == "SetRandomStrengthAdd":
                self._client.set_random_strength(add=int(av))
            elif action == "SetRandomStrengthSub":
                self._client.set_random_strength(sub=int(av))
        except Exception as e:
            logger.warning(f"动作执行失败 [{action}]: {e}")

    def get_status_text(self) -> str:
        """获取监控器状态文本。"""
        if not self._enabled:
            return "未启动"
        parts = [f"运行中 ({len(self._monitors)}项)"]
        if self._trigger_log:
            parts.append(f"触发{len(self._trigger_log)}次")
        return " | ".join(parts)

    def get_monitor_summary(self) -> list:
        """获取所有监控项的摘要信息。"""
        result = []
        for i, m in enumerate(self._monitors):
            result.append({
                "index": i,
                "field": m.field_key,
                "scenario": m.scenario,
                "action": m.action,
                "action_mode": m.action_mode,
                "action_value": m.action_value,
                "time": m.time_ms,
                "prev_value": m._prev_value,
                "max_value": m._max_value,
                "current_value": self._values.get(m.field_key),
            })
        return result

    # ── 死亡统计（供 UI 事件触发使用）──

    def get_death_stats(self) -> dict:
        """获取死亡统计数据。

        基于共享内存中的 dead_count 和 self_hp 推算：
        - self_hp 降为 0 → 自己死亡
        - dead_count 增加 + 自己未死 → 队友死亡
        """
        with self._lock:
            # 当前总死亡数
            total_dead = int(self._values.get("dead_count", 0))
            # 自己血量百分比
            self_hp_pct = float(self._values.get("self_hp_percent", 100.0))
            self_hp = int(self._values.get("self_current_hp", -1))

            # 初始化死亡追踪状态
            if not hasattr(self, "_death_state"):
                self._death_state = {
                    "self_deaths": 0,
                    "mate_deaths": 0,
                    "last_total_dead": 0,
                    "last_self_dead": False,
                    "last_self_deaths": 0,
                    "last_mate_deaths": 0,
                    "hp_low_triggered": False,
                }

            ds = self._death_state

            # 检测自己死亡：血量降为 0（从非0状态）
            self_dead_now = (self_hp == 0 or self_hp_pct <= 0.1)
            if self_dead_now and not ds["last_self_dead"]:
                ds["self_deaths"] += 1
            ds["last_self_dead"] = self_dead_now

            # 检测队友死亡：总死亡数增加，但自己没死
            if total_dead > ds["last_total_dead"]:
                delta = total_dead - ds["last_total_dead"]
                if not self_dead_now:
                    # 自己没死，增加的是队友死亡
                    ds["mate_deaths"] += delta
                else:
                    # 自己死了，如果 delta > 1 则多出的是队友死亡
                    if delta > 1:
                        ds["mate_deaths"] += (delta - 1)
            ds["last_total_dead"] = total_dead

            return {
                "self_deaths": ds["self_deaths"],
                "mate_deaths": ds["mate_deaths"],
                "last_self_deaths": ds["last_self_deaths"],
                "last_mate_deaths": ds["last_mate_deaths"],
                "self_hp_percent": self_hp_pct,
                "hp_low_triggered": ds["hp_low_triggered"],
            }

    def update_last_deaths(self):
        """更新上次死亡数（触发后调用，避免重复触发）。"""
        if hasattr(self, "_death_state"):
            ds = self._death_state
            ds["last_self_deaths"] = ds["self_deaths"]
            ds["last_mate_deaths"] = ds["mate_deaths"]

    def set_hp_low_triggered(self, triggered: bool):
        """设置血量低已触发标记。"""
        if hasattr(self, "_death_state"):
            self._death_state["hp_low_triggered"] = triggered

    def reset_death_stats(self):
        """重置死亡统计。"""
        if hasattr(self, "_death_state"):
            self._death_state = {
                "self_deaths": 0,
                "mate_deaths": 0,
                "last_total_dead": int(self._values.get("dead_count", 0)),
                "last_self_dead": False,
                "last_self_deaths": 0,
                "last_mate_deaths": 0,
                "hp_low_triggered": False,
            }


# 全局单例
_default_monitor = None


def get_default_monitor() -> GameValueMonitor:
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = GameValueMonitor()
    return _default_monitor
