"""郊狼 Coyote 设备抽象层。

实现设备管理：多客户端、强度控制、波形播放、一键开火等。
支持两种后端：
 1. 模拟模式（MockDevice）——无真实设备时用于调试
 2. (未来) DG-Lab Socket / 蓝牙 / WebSocket 连接真实设备

所有强度范围：0-200 (strength: 0-40, *5 映射到真实设备)
"""
import time
import threading
import uuid
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── 内置波形库（完整 18 个，源自 DGLabGameController/Data/CoyoteGameHub/data/pulse.json5）──
BUILTIN_PULSES = [
    {
        "id": "d6f83af0",
        "name": "呼吸",
        "pulseData": [
            "0A0A0A0A00000000", "0A0A0A0A14141414", "0A0A0A0A28282828",
            "0A0A0A0A3C3C3C3C", "0A0A0A0A50505050", "0A0A0A0A64646464",
            "0A0A0A0A64646464", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "7eae1e5f",
        "name": "潮汐",
        "pulseData": [
            "0A0A0A0A00000000", "0D0D0D0D0F0F0F0F", "101010101E1E1E1E",
            "1313131332323232", "1616161641414141", "1A1A1A1A50505050",
            "1D1D1D1D64646464", "202020205A5A5A5A", "2323232350505050",
            "262626264B4B4B4B", "2A2A2A2A41414141", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "eea0e4ce",
        "name": "连击",
        "pulseData": [
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A41414141", "0A0A0A0A1E1E1E1E", "0A0A0A0A00000000",
            "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A41414141",
            "0A0A0A0A1E1E1E1E", "0A0A0A0A00000000", "0A0A0A0A00000000",
            "0A0A0A0A00000000",
        ],
    },
    {
        "id": "2cbd592e",
        "name": "快速按捏",
        "pulseData": [
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A00000000",
        ],
    },
    {
        "id": "d99221f6",
        "name": "按捏渐强",
        "pulseData": [
            "0A0A0A0A00000000", "0A0A0A0A19191919", "0A0A0A0A00000000",
            "0A0A0A0A32323232", "0A0A0A0A00000000", "0A0A0A0A46464646",
            "0A0A0A0A00000000", "0A0A0A0A55555555", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "cd9868d3",
        "name": "心跳节奏",
        "pulseData": [
            "7070707064646464", "7070707064646464", "0A0A0A0A00000000",
            "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
            "0A0A0A0A00000000", "0A0A0A0A46464646", "0A0A0A0A50505050",
            "0A0A0A0A5A5A5A5A", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
            "0A0A0A0A00000000", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "dc033dc1",
        "name": "压缩",
        "pulseData": [
            "4A4A4A4A64646464", "4545454564646464", "4040404064646464",
            "3B3B3B3B64646464", "3636363664646464", "3232323264646464",
            "2D2D2D2D64646464", "2828282864646464", "2323232364646464",
            "1E1E1E1E64646464", "1A1A1A1A64646464", "0A0A0A0A64646464",
            "0A0A0A0A64646464", "0A0A0A0A64646464", "0A0A0A0A64646464",
            "0A0A0A0A64646464", "0A0A0A0A64646464", "0A0A0A0A64646464",
            "0A0A0A0A64646464", "0A0A0A0A64646464", "0A0A0A0A64646464",
        ],
    },
    {
        "id": "9be2ec50",
        "name": "节奏步伐",
        "pulseData": [
            "0A0A0A0A00000000", "0A0A0A0A14141414", "0A0A0A0A28282828",
            "0A0A0A0A3C3C3C3C", "0A0A0A0A50505050", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A19191919", "0A0A0A0A32323232",
            "0A0A0A0A4B4B4B4B", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A1E1E1E1E", "0A0A0A0A41414141", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "ec8c5e88",
        "name": "颗粒摩擦",
        "pulseData": [
            "0A0A0A0A64646464", "0D0D0D0D64646464", "1010101064646464",
            "1414141400000000", "1717171764646464", "1B1B1B1B64646464",
            "1E1E1E1E64646464", "2222222200000000", "2525252564646464",
            "2929292964646464", "2C2C2C2C64646464", "3030303000000000",
        ],
    },
    {
        "id": "00337ed4",
        "name": "渐变弹跳",
        "pulseData": [
            "0A0A0A0A00000000", "0A0A0A0A1E1E1E1E", "0B0B0B0B41414141",
            "0C0C0C0C64646464", "0D0D0D0D00000000", "0E0E0E0E1E1E1E1E",
            "0F0F0F0F41414141", "1010101064646464", "1111111100000000",
            "121212121E1E1E1E", "1313131341414141", "1414141464646464",
            "1515151500000000", "161616161E1E1E1E", "1717171741414141",
            "1818181864646464", "1919191900000000", "1A1A1A1A1E1E1E1E",
            "1B1B1B1B41414141", "1C1C1C1C64646464", "1D1D1D1D00000000",
            "1E1E1E1E1E1E1E1E", "1F1F1F1F41414141", "2020202064646464",
            "2121212100000000", "222222221E1E1E1E", "2323232341414141",
            "2424242464646464", "2525252500000000", "262626261E1E1E1E",
            "2727272741414141", "2828282864646464", "0A0A0A0A00000000",
            "0A0A0A0A00000000",
        ],
    },
    {
        "id": "bd272001",
        "name": "波浪涟漪",
        "pulseData": [
            "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
            "0A0A0A0A46464646", "0A0A0A0A00000000", "0A0A0A0A32323232",
            "0A0A0A0A64646464", "0A0A0A0A46464646", "0A0A0A0A00000000",
            "0A0A0A0A32323232", "0A0A0A0A64646464", "0A0A0A0A46464646",
            "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
            "0A0A0A0A46464646", "0A0A0A0A00000000", "0A0A0A0A32323232",
            "0A0A0A0A64646464", "0A0A0A0A46464646", "0A0A0A0A00000000",
            "0A0A0A0A32323232", "0A0A0A0A64646464", "0A0A0A0A46464646",
            "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
            "0A0A0A0A46464646", "0A0A0A0A00000000", "0A0A0A0A32323232",
            "0A0A0A0A64646464", "0A0A0A0A46464646", "0A0A0A0A00000000",
            "0A0A0A0A32323232", "0A0A0A0A64646464", "0A0A0A0A46464646",
            "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
            "0A0A0A0A46464646", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "526fb051",
        "name": "雨水冲刷",
        "pulseData": [
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "0E0E0E0E1E1E1E1E", "0E0E0E0E41414141", "0E0E0E0E64646464",
            "3A3A3A3A64646464", "3A3A3A3A64646464", "3A3A3A3A64646464",
            "3A3A3A3A64646464", "3A3A3A3A64646464", "3A3A3A3A64646464",
            "3A3A3A3A64646464", "3A3A3A3A64646464", "3A3A3A3A64646464",
            "3A3A3A3A64646464", "3A3A3A3A64646464", "3A3A3A3A64646464",
            "3A3A3A3A64646464", "3A3A3A3A64646464", "3A3A3A3A64646464",
            "3A3A3A3A64646464", "3A3A3A3A64646464", "3A3A3A3A64646464",
            "3A3A3A3A64646464", "3A3A3A3A64646464", "0A0A0A0A00000000",
            "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "32656da9",
        "name": "变速敲击",
        "pulseData": [
            "1818181864646464", "1818181864646464", "1818181864646464",
            "1818181800000000", "1818181800000000", "1818181800000000",
            "1818181800000000", "1818181864646464", "1818181864646464",
            "1818181864646464", "1818181800000000", "1818181800000000",
            "1818181800000000", "1818181800000000", "1818181864646464",
            "1818181864646464", "1818181864646464", "1818181800000000",
            "1818181800000000", "1818181800000000", "1818181800000000",
            "1818181864646464", "1818181864646464", "1818181864646464",
            "1818181800000000", "1818181800000000", "1818181800000000",
            "1818181800000000", "7070707064646464", "7070707064646464",
            "7070707064646464", "7070707064646464", "7070707064646464",
            "7070707064646464", "7070707064646464", "7070707064646464",
            "7070707064646464", "7070707064646464", "7070707064646464",
            "7070707064646464", "7070707064646464", "7070707064646464",
            "7070707064646464", "7070707064646464", "7070707064646464",
            "7070707064646464", "7070707064646464", "7070707064646464",
            "7070707064646464", "7070707064646464", "7070707064646464",
            "7070707064646464", "0A0A0A0A00000000", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "c93b68ec",
        "name": "信号灯",
        "pulseData": [
            "BEBEBEBE64646464", "BEBEBEBE64646464", "BEBEBEBE64646464",
            "BEBEBEBE64646464", "BEBEBEBE64646464", "BEBEBEBE64646464",
            "BEBEBEBE64646464", "BEBEBEBE64646464", "BEBEBEBE64646464",
            "BEBEBEBE64646464", "BEBEBEBE64646464", "BEBEBEBE64646464",
            "0A0A0A0A00000000", "101010101E1E1E1E", "1717171741414141",
            "1E1E1E1E64646464", "0A0A0A0A00000000", "101010101E1E1E1E",
            "1717171741414141", "1E1E1E1E64646464", "0A0A0A0A00000000",
            "101010101E1E1E1E", "1717171741414141", "1E1E1E1E64646464",
        ],
    },
    {
        "id": "ec329704",
        "name": "挑逗1",
        "pulseData": [
            "0A0A0A0A00000000", "0C0C0C0C19191919", "0E0E0E0E32323232",
            "101010104B4B4B4B", "1212121264646464", "1515151564646464",
            "1717171764646464", "1919191900000000", "1B1B1B1B00000000",
            "1E1E1E1E00000000", "0A0A0A0A00000000", "0C0C0C0C19191919",
            "0E0E0E0E32323232", "101010104B4B4B4B", "1212121264646464",
            "1515151564646464", "1717171764646464", "1919191900000000",
            "1B1B1B1B00000000", "1E1E1E1E00000000", "0A0A0A0A00000000",
            "0C0C0C0C19191919", "0E0E0E0E32323232", "101010104B4B4B4B",
            "1212121264646464", "1515151564646464", "1717171764646464",
            "1919191900000000", "1B1B1B1B00000000", "1E1E1E1E00000000",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
            "0A0A0A0A64646464", "0A0A0A0A00000000",
        ],
    },
    {
        "id": "f2679890",
        "name": "挑逗2",
        "pulseData": [
            "2525252500000000", "222222220A0A0A0A", "2020202014141414",
            "1E1E1E1E1E1E1E1E", "1B1B1B1B2D2D2D2D", "1919191937373737",
            "1717171741414141", "141414144B4B4B4B", "1212121255555555",
            "1010101064646464", "2525252500000000", "222222220A0A0A0A",
            "2020202014141414", "1E1E1E1E1E1E1E1E", "1B1B1B1B2D2D2D2D",
            "1919191937373737", "1717171741414141", "141414144B4B4B4B",
            "1212121255555555", "1010101064646464", "0A0A0A0A64646464",
            "0A0A0A0A00000000", "0B0B0B0B64646464", "0C0C0C0C00000000",
            "0D0D0D0D64646464", "0E0E0E0E00000000", "0F0F0F0F64646464",
            "1010101000000000", "1010101064646464", "1111111100000000",
            "1212121264646464", "1313131300000000", "1414141464646464",
            "1515151500000000", "1616161664646464", "1717171700000000",
            "1717171764646464", "1818181800000000", "1919191964646464",
            "1A1A1A1A00000000", "1B1B1B1B64646464", "1C1C1C1C00000000",
            "1D1D1D1D64646464", "1E1E1E1E00000000", "0A0A0A0A00000000",
            "0A0A0A0A00000000",
        ],
    },
]


def get_pulse_by_id(pid: str) -> Optional[dict]:
    """根据 ID 查找波形，先在内置波形中找，再在导入波形中找。"""
    for p in BUILTIN_PULSES:
        if p["id"] == pid:
            return p
    for p in _imported_pulses:
        if p["id"] == pid:
            return p
    return None


# 导入波形列表（运行时由 pulse_loader 填充）
_imported_pulses: list = []


def add_imported_pulse(pulse: dict) -> str:
    """添加一个导入的波形，返回其 id。"""
    pid = pulse.get("id") or str(uuid.uuid4())[:8]
    pulse["id"] = pid
    # 去重：相同 id 替换
    for i, p in enumerate(_imported_pulses):
        if p["id"] == pid:
            _imported_pulses[i] = pulse
            return pid
    _imported_pulses.append(pulse)
    return pid


def clear_imported_pulses():
    """清空所有导入的波形。"""
    _imported_pulses.clear()


def all_pulses() -> list:
    """返回所有可用波形（内置 + 导入）。"""
    return list(BUILTIN_PULSES) + list(_imported_pulses)


class CoyoteClient:
    """单个郊狼客户端（连接的一台设备）。

    支持两种后端：
    - mock=True（默认）：模拟设备，仅本地状态变化
    - mock=False：通过 WebSocket 连接真实 DG-Lab APP → 蓝牙 → 郊狼设备
    """

    def __init__(self, client_id: str = None, name: str = "未命名设备",
                 mock: bool = True):
        self.client_id = client_id or str(uuid.uuid4())
        self.name = name
        self.connected = True
        self.mock = mock  # True=模拟设备, False=真实WebSocket设备
        self.ws_client_id = None  # WebSocket 客户端 ID（真实设备时使用）
        self.battery = 100  # 0-100
        self.signal = 100   # 0-100

        # A / B 通道强度 (0-200，和官方一致)
        self._strength_a = 0
        self._strength_b = 0
        self._limit = 200

        # 基础配置（服务端强度）
        self.base_strength = 5      # strength config
        self.random_strength = 5    # random strength
        self.enable_b_channel = False
        self.b_channel_multiplier = 1.0

        # 波形
        self.current_pulse_id = "d6f83af0"
        self.pulse_mode = "single"  # single / sequence / random
        self.pulse_list_ids = [p["id"] for p in BUILTIN_PULSES]
        self.pulse_change_interval = 60

        # 一键开火状态
        self.fire_end_ts = 0  # 开火结束时间戳
        self.fire_strength = 0
        self._fire_lock = threading.Lock()

        # 事件回调（供上层监听）
        self.on_change = None  # callable(client, change_type, value)

    # ── WebSocket 真实设备通信（mock=False 时使用） ──
    def _ws_set_strength(self):
        """真实设备：通过 WebSocket 发送当前强度指令（A / B 通道）。"""
        if not self.ws_client_id:
            logger.warning("[_ws_set_strength] ws_client_id 为空，跳过发送")
            return
        try:
            import coyote_ws_server
            ws_mgr = coyote_ws_server.get_default_ws_manager()
            if not ws_mgr.running:
                logger.warning("[_ws_set_strength] WS 服务未运行，跳过发送")
                return
            # A 通道 (CH_A=1)
            ws_mgr.send_strength_async(self.ws_client_id, 1, self._strength_a)
            # B 通道 (CH_B=2)，仅在启用 B 通道时发送
            if self.enable_b_channel:
                ws_mgr.send_strength_async(self.ws_client_id, 2, self._strength_b)
        except Exception as e:
            logger.exception(f"[_ws_set_strength] 发送失败: {e}")

    def _ws_fire(self, strength: int, time_ms: int, pulse_id: str = None):
        """真实设备：通过 WebSocket 发送开火指令。强度范围 0-40（内部 ×5 → 0-200）。"""
        if not self.ws_client_id:
            logger.warning("[_ws_fire] ws_client_id 为空，开火爆破！")
            return
        try:
            import coyote_ws_server
            ws_mgr = coyote_ws_server.get_default_ws_manager()
            if not ws_mgr.running:
                logger.warning("[_ws_fire] WS 服务未运行，开火被丢弃")
                return
            # 0-40 → 0-200
            actual_strength = max(0, min(200, strength * 5))
            # 解析目标波形 hex 数据
            pulse_hex_list = None
            if pulse_id:
                pulse = get_pulse_by_id(pulse_id)
                if pulse:
                    pulse_hex_list = pulse["pulseData"]
                else:
                    logger.warning(f"[_ws_fire] 找不到波形 id={pulse_id}，将使用默认波形")
            logger.info(
                f"[_ws_fire] 提交开火: client_id={self.ws_client_id[:8] if self.ws_client_id else ''}… "
                f"strength(0-40)={strength} → actual(0-200)={actual_strength} "
                f"time_ms={time_ms} pulse_id={pulse_id or '默认'}")
            ws_mgr.fire_async(self.ws_client_id, actual_strength, time_ms, pulse_hex_list)
        except Exception as e:
            logger.exception(f"[_ws_fire] 提交失败: {e}")

    # ── 强度限制 ──
    @property
    def strength_a(self) -> int:
        return self._strength_a

    @strength_a.setter
    def strength_a(self, v: int):
        self._strength_a = max(0, min(self._limit, int(v)))
        if self.on_change:
            self.on_change(self, "strength_a", self._strength_a)

    @property
    def strength_b(self) -> int:
        return self._strength_b

    @strength_b.setter
    def strength_b(self, v: int):
        self._strength_b = max(0, min(self._limit, int(v)))
        if self.on_change:
            self.on_change(self, "strength_b", self._strength_b)

    @property
    def strength_limit(self) -> int:
        return self._limit

    @strength_limit.setter
    def strength_limit(self, v: int):
        """设置强度上限（0-200），并立即钳制当前强度值。"""
        self._limit = max(0, min(200, int(v)))
        # 立即钳制当前强度，确保不超过新上限
        self._strength_a = max(0, min(self._limit, self._strength_a))
        self._strength_b = max(0, min(self._limit, self._strength_b))
        if self.on_change:
            self.on_change(self, "strength_limit", self._limit)
            self.on_change(self, "strength_a", self._strength_a)
            self.on_change(self, "strength_b", self._strength_b)

    # ── 动作：强度 ──
    def set_strength(self, strength: dict = None, random_strength: dict = None):
        """SetStrengthSet/Add/Sub 动作。"""
        if strength:
            if "set" in strength:
                self.base_strength = max(0, min(40, int(strength["set"])))
            if "add" in strength:
                self.base_strength = max(0, min(40, self.base_strength + int(strength["add"])))
            if "sub" in strength:
                self.base_strength = max(0, min(40, self.base_strength - int(strength["sub"])))
            # 映射 base_strength (0-40) * 5 到客户端强度 (0-200)
            target = self.base_strength * 5
            self.strength_a = target
            if self.enable_b_channel:
                self.strength_b = int(target * self.b_channel_multiplier)
            # 真实设备：通过 WebSocket 发送强度指令
            if not self.mock:
                self._ws_set_strength()
            if self.on_change:
                self.on_change(self, "base_strength", self.base_strength)

        if random_strength:
            if "set" in random_strength:
                self.random_strength = max(0, min(40, int(random_strength["set"])))
            if "add" in random_strength:
                self.random_strength = max(0, min(40, self.random_strength + int(random_strength["add"])))
            if "sub" in random_strength:
                self.random_strength = max(0, min(40, self.random_strength - int(random_strength["sub"])))
            if self.on_change:
                self.on_change(self, "random_strength", self.random_strength)

    def set_random_strength(self, set_=None, add=None, sub=None):
        """SetRandomStrengthXxx 快捷方法。"""
        rs = {}
        if set_ is not None:
            rs["set"] = set_
        if add is not None:
            rs["add"] = add
        if sub is not None:
            rs["sub"] = sub
        self.set_strength(random_strength=rs)

    # ── 动作：波形 ──
    def set_pulse(self, pulse_id):
        if isinstance(pulse_id, list):
            self.pulse_list_ids = [p for p in pulse_id if get_pulse_by_id(p)]
            self.pulse_mode = "sequence"
            self.current_pulse_id = self.pulse_list_ids[0] if self.pulse_list_ids else "d6f83af0"
        else:
            self.current_pulse_id = pulse_id
            self.pulse_mode = "single"
        if self.on_change:
            self.on_change(self, "pulse", self.current_pulse_id)

    # ── 动作：一键开火 ──
    def fire(self, strength: int = 20, time_ms: int = 5000,
             override: bool = False, pulse_id: str = None) -> bool:
        """一键开火。强度 0-40，时间 ms，最高 30000。"""
        strength = max(0, min(40, int(strength)))
        time_ms = max(100, min(30000, int(time_ms)))
        with self._fire_lock:
            now = time.time()
            end = now + time_ms / 1000.0
            if self.fire_end_ts > now and not override:
                # 叠加时间
                self.fire_end_ts = min(self.fire_end_ts + time_ms / 1000.0, now + 60)
            else:
                self.fire_end_ts = end
            self.fire_strength = strength

            # 实际强度 = fire_strength * 5
            actual = strength * 5
            self.strength_a = actual
            if self.enable_b_channel:
                self.strength_b = int(actual * self.b_channel_multiplier)

            if pulse_id:
                self.set_pulse(pulse_id)

            # 真实设备：通过 WebSocket 发送开火指令
            if not self.mock:
                self._ws_fire(strength, time_ms, pulse_id)

            if self.on_change:
                self.on_change(self, "fire", {"strength": strength, "time_ms": time_ms})
            return True

    @property
    def is_firing(self) -> bool:
        with self._fire_lock:
            return time.time() < self.fire_end_ts

    # ── 游戏配置 JSON 输出 ──
    def to_game_info(self) -> dict:
        return {
            "status": 1,
            "code": "OK",
            "strengthConfig": {
                "strength": self.base_strength,
                "randomStrength": self.random_strength,
            },
            "gameConfig": {
                "strengthChangeInterval": [15, 30],
                "enableBChannel": self.enable_b_channel,
                "bChannelStrengthMultiplier": self.b_channel_multiplier,
                "pulseId": (self.pulse_list_ids if self.pulse_mode != "single"
                            else self.current_pulse_id),
                "pulseMode": self.pulse_mode,
                "pulseChangeInterval": self.pulse_change_interval,
            },
            "clientStrength": {
                "strength": max(self._strength_a, self._strength_b),
                "limit": self._limit,
            },
            "currentPulseId": self.current_pulse_id,
        }


class CoyoteDeviceManager:
    """郊狼设备管理器：多客户端管理 + 模拟/真实后端切换。"""

    def __init__(self):
        self._clients: dict[str, CoyoteClient] = {}
        self._lock = threading.Lock()
        self._allow_broadcast = True
        self._log = []  # (timestamp, type, client_id, detail)
        self._on_client_change = None  # 回调
        # 不再创建默认的模拟客户端——只接收 APP 扫码连接的真实设备

    # ── 客户端管理 ──
    def add_client(self, client: CoyoteClient) -> str:
        client.on_change = self._on_client_evt
        with self._lock:
            self._clients[client.client_id] = client
            self._log_evt("client", client.client_id, "已连接")
        return client.client_id

    def remove_client(self, cid: str):
        with self._lock:
            if cid in self._clients:
                del self._clients[cid]
                self._log_evt("client", cid, "已断开")

    def get_client(self, cid: str) -> Optional[CoyoteClient]:
        with self._lock:
            return self._clients.get(cid)

    def list_clients(self) -> list:
        with self._lock:
            return [
                {
                    "id": c.client_id,
                    "name": c.name,
                    "connected": c.connected,
                    "battery": c.battery,
                    "signal": c.signal,
                    "strengthA": c.strength_a,
                    "strengthB": c.strength_b,
                    "limit": c.strength_limit,
                    "pulse": c.current_pulse_id,
                    "firing": c.is_firing,
                }
                for c in self._clients.values()
            ]

    def resolve_client_ids(self, cid: str) -> list[str]:
        """解析 clientId（'all' -> 广播）。"""
        with self._lock:
            if cid == "all":
                if not self._allow_broadcast:
                    return []
                return list(self._clients.keys())
            return [cid] if cid in self._clients else []

    # ── 批量 API ──
    def broadcast_set_strength(self, cid: str, **kwargs) -> list[str]:
        """广播 set_strength，返回成功的 clientId 列表。"""
        ids = self.resolve_client_ids(cid)
        ok = []
        for i in ids:
            c = self.get_client(i)
            if c:
                c.set_strength(**kwargs)
                ok.append(i)
                self._log_evt("strength", i, f"set_strength {kwargs}")
        return ok

    def broadcast_set_pulse(self, cid: str, pulse_id) -> list[str]:
        ids = self.resolve_client_ids(cid)
        ok = []
        for i in ids:
            c = self.get_client(i)
            if c:
                c.set_pulse(pulse_id)
                ok.append(i)
                self._log_evt("pulse", i, f"pulse={pulse_id}")
        return ok

    def broadcast_fire(self, cid: str, **kwargs) -> list[str]:
        ids = self.resolve_client_ids(cid)
        ok = []
        for i in ids:
            c = self.get_client(i)
            if c:
                c.fire(**kwargs)
                ok.append(i)
                self._log_evt("fire", i,
                              f"strength={kwargs.get('strength')} time={kwargs.get('time_ms')}")
        return ok

    def set_all_strength_limit(self, limit: int):
        """批量设置所有客户端的强度上限。"""
        with self._lock:
            for c in self._clients.values():
                c.strength_limit = limit
            self._log_evt("system", "all", f"强度上限设为 {limit}")

    def set_all_pulse(self, pulse_id: str):
        """批量设置所有客户端的当前波形。"""
        with self._lock:
            for c in self._clients.values():
                c.set_pulse(pulse_id)
            self._log_evt("pulse", "all", f"波形切换为 {pulse_id}")

    # ── 事件/日志 ──
    def _on_client_evt(self, client: CoyoteClient, change_type: str, value):
        self._log_evt(change_type, client.client_id, f"value={value}")
        if self._on_client_change:
            try:
                self._on_client_change(client, change_type, value)
            except Exception:
                pass

    def _log_evt(self, typ: str, cid: str, detail: str):
        ts = time.strftime("%H:%M:%S")
        self._log.append((ts, typ, cid, detail))
        if len(self._log) > 300:
            self._log = self._log[-300:]

    @property
    def logs(self):
        return list(self._log)

    # ── 波形库 ──
    @property
    def pulse_list(self) -> list:
        return [{"id": p["id"], "name": p["name"]} for p in BUILTIN_PULSES]


# 全局单例
_default_manager = None


def get_default_manager() -> CoyoteDeviceManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = CoyoteDeviceManager()
    return _default_manager
