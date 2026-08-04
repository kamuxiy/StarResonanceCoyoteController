"""
网络抓包模块 - 替代 OCR 方案获取游戏血量数据。

协议格式 (已确认, 来自实测):
  [4B BE total_len]  - 消息总长度 (opcode+header+payload, 不含自身)
  [2B BE opcode]     - 消息类型 (1=请求C->S, 2=响应S->C, 3=ACK, 4=心跳)
  [20B header]       - flags(4B) + req_id(4B) + ack_id(4B) + channel(4B) + seq(4B)
  [protobuf payload] - Google.Protobuf 消息体 (可能zstd压缩)

目标服务器: 58.217.183.115 (TCP, 端口动态)
游戏进程: Star.exe
"""
import os
import sys
import time
import struct
import socket
import ctypes
import threading
import traceback
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Callable, Dict, List, Tuple, Any
from dataclasses import dataclass, field

try:
    from scapy.all import sniff, IP, TCP, Raw, get_if_list, conf
    SCAPY_AVAILABLE = True
except ImportError:
    SCAPY_AVAILABLE = False

from proto_parser import parse_protobuf, ParsedMessage, try_zstd_decompress, find_hp_candidates
import app_paths

TARGET_IP = "58.217.183.115"

# StarResonanceDamageCounter (Node.js 源码版) 路径和API
#   打包时：exe 同级的 StarResonanceDamageCounter-master/ 优先；其次打包内置；最后开发 E:\CODE
SRDC_DIR = app_paths.srdc_dir()
SRDC_SERVER_JS = app_paths.srdc_server_js()
SRDC_API_URL = app_paths.srdc_api_url()


class RealtimeCaptureReader(threading.Thread):
    """
    启动 C# 实时抓包工具并监控其输出的 realtime_hp.json。

    C# 工具直接复用 StarResonanceDpsAnalysis 的 DLL（Core.dll + Proto.dll），
    使用 SharpPcap 抓包，TcpStreamProcessor 解析协议，DataStorage 事件获取实时血量。
    """

    def __init__(self, interface: Optional[str] = None,
                 on_state: Optional[Callable] = None,
                 on_log: Optional[Callable] = None,
                 on_error: Optional[Callable] = None,
                 on_status: Optional[Callable] = None,
                 poll_interval: float = 1.0):
        super().__init__(daemon=True)
        self.interface = interface
        self.on_state = on_state
        self.on_log = on_log
        self.on_error = on_error
        self.on_status = on_status
        self.poll_interval = poll_interval

        self.running = False
        self._process = None
        self._last_mtime = 0
        self._last_hp = 0

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        full_msg = f"[抓包 {ts}] {msg}"
        print(full_msg, flush=True)
        if self.on_log:
            try:
                self.on_log(full_msg)
            except Exception:
                pass

    def run(self):
        self.running = True

        # 检查 C# 工具是否已编译
        if not os.path.exists(REALTIME_CAPTURE_DLL):
            self.log("[错误] C# 抓包工具未编译，请先运行: cd RealtimeCapture && dotnet build -c Release")
            if self.on_error:
                self.on_error("C# 抓包工具未编译")
            return

        # 检查游戏是否在运行
        self._check_game_process()

        # 检查 C# 工具是否已在运行
        import subprocess
        exe_path = os.path.join(REALTIME_CAPTURE_DIR, "bin", "Release", "RealtimeCapture.exe")
        cs_pid = self._find_running_capture()

        if cs_pid:
            self.log(f"C# 抓包工具已在运行 (PID={cs_pid})")
        else:
            # 尝试启动 C# 工具
            self.log(f"启动 C# 实时抓包工具: {exe_path}")
            try:
                # 尝试不同的启动方式
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                self._process = subprocess.Popen(
                    [exe_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=CREATE_NEW_PROCESS_GROUP
                )
                self.log(f"C# 抓包工具已启动 (PID={self._process.pid})")
            except Exception as e:
                self.log(f"[警告] 自动启动失败: {e}")
                self.log(f"请手动启动: {exe_path}")
                self.log("C# 工具启动后，将自动监控 realtime_hp.json")

        if self.on_status:
            self.on_status("running")

        # 同时监控 C# 工具输出和 realtime_hp.json
        import json
        import threading as th

        # 线程1: 如果是我们自己启动的，读取 C# 工具的标准输出
        if self._process:
            def read_output():
                try:
                    for line in self._process.stdout:
                        line = line.strip()
                        if line:
                            self.log(f"[C#] {line}")
                except Exception:
                    pass

            output_thread = th.Thread(target=read_output, daemon=True)
            output_thread.start()

        # 主循环: 监控 realtime_hp.json
        while self.running and (self._process is None or self._process.poll() is None):
            try:
                if os.path.exists(REALTIME_HP_FILE):
                    mtime = os.path.getmtime(REALTIME_HP_FILE)
                    if mtime != self._last_mtime:
                        self._last_mtime = mtime
                        with open(REALTIME_HP_FILE, 'r', encoding='utf-8') as f:
                            data = json.load(f)

                        hp = data.get("hp", 0)
                        if hp != self._last_hp:
                            self._last_hp = hp
                            phi = PlayerHealthInfo(
                                name=data.get("name", ""),
                                uid=data.get("uid", 0),
                                current_hp=hp,
                                max_hp=data.get("max_hp", 0),
                                health_percent=data.get("percent", 0),
                                is_self=True
                            )
                            self.log(
                                f"血量更新: {phi.name} "
                                f"HP={phi.current_hp:,}/{phi.max_hp:,} "
                                f"({phi.health_percent:.1f}%)"
                            )
                            if self.on_state:
                                try:
                                    self.on_state(phi)
                                except Exception:
                                    pass
            except Exception as e:
                self.log(f"读取血量数据异常: {e}")

            time.sleep(self.poll_interval)

        # 清理
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except Exception:
                self._process.kill()

        if self.on_status:
            self.on_status("stopped")
        self.log("C# 抓包工具已停止")

    def stop(self):
        self.running = False
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass

    def _find_running_capture(self) -> Optional[int]:
        """查找已运行的 RealtimeCapture 进程 PID"""
        try:
            import psutil
            for proc in psutil.process_iter(['name', 'pid']):
                if proc.info['name'] == 'RealtimeCapture.exe':
                    return proc.info['pid']
        except Exception:
            pass
        return None

    def _check_game_process(self):
        """检查游戏进程是否在运行"""
        try:
            import psutil
            for proc in psutil.process_iter(['name', 'pid']):
                if proc.info['name'] and proc.info['name'] == 'Star.exe':
                    self.log(f"检测到游戏进程: Star.exe (PID={proc.info['pid']})")
                    return True
            self.log("[警告] 未检测到游戏进程(Star.exe)，请先启动游戏!")
            return False
        except Exception:
            return True

    def get_stats(self) -> Dict:
        return {
            "packets": 0,
            "bytes": 0,
            "messages": 0,
            "flows": 0,
            "opcode_count": 0,
            "hp": self._last_hp,
            "max_hp": 0,
            "hp_percent": 0,
            "player_name": "",
        }


class BattleHistoryReader(threading.Thread):
    """
    读取 StarResonanceDpsAnalysis 输出的 BattleHistory JSON 获取血量。

    StarResonanceDpsAnalysis 已经能正确抓包、解密、解析游戏协议，
    它在战斗结束时将玩家数据（含 HP/MaxHP）保存为 JSON。
    本类监控其输出目录，实时读取最新血量数据。
    """

    def __init__(self, battle_dir: str = None,
                 on_state: Optional[Callable] = None,
                 on_log: Optional[Callable] = None,
                 on_error: Optional[Callable] = None,
                 on_status: Optional[Callable] = None,
                 on_srda_status: Optional[Callable] = None,
                 poll_interval: float = 2.0):
        super().__init__(daemon=True)
        self.battle_dir = battle_dir or SRDA_BATTLE_DIR
        self.on_state = on_state
        self.on_log = on_log
        self.on_error = on_error
        self.on_status = on_status
        self.on_srda_status = on_srda_status
        self.poll_interval = poll_interval

        self.running = False
        self._last_file = None
        self._last_mtime = 0
        self._last_hp = -1
        self._self_uid = self._read_self_uid()
        self._process = None
        self._started_by_us = False
        self._srda_pid = None
        self._last_data_time = None
        self._status_check_counter = 0
        self._start_time = None
        self._default_sent = False

    def _read_self_uid(self) -> int:
        """从 appsettings.json 读取自身玩家 UID"""
        import json
        config_path = os.path.join(SRDA_DIR, "appsettings.json")
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            uid = config.get("Config", {}).get("uid", 0)
            if uid:
                self.log(f"自身 UID: {uid}")
            return uid
        except Exception:
            return 0

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        full_msg = f"[SRDA {ts}] {msg}"
        print(full_msg, flush=True)
        if self.on_log:
            try:
                self.on_log(full_msg)
            except Exception:
                pass

    def run(self):
        self.running = True
        self._start_time = time.time()
        if self.on_status:
            self.on_status("running")

        self.log(f"BattleHistory 监控启动: {self.battle_dir}")

        if not os.path.isdir(self.battle_dir):
            self.log(f"[错误] 目录不存在: {self.battle_dir}")
            if self.on_error:
                self.on_error(f"目录不存在: {self.battle_dir}")
            return

        # 检查 SRDA 程序是否在运行，不在的话尝试启动
        try:
            import psutil
            srda_running = False
            for proc in psutil.process_iter(['name', 'pid']):
                try:
                    name = proc.info['name'] or ''
                    if 'StarResonanceDpsAnalysis' in name and 'WPF' in name:
                        srda_running = True
                        self._srda_pid = proc.info['pid']
                        self.log(f"检测到 SRDA 进程 (PID={self._srda_pid})")
                        self._emit_srda_status("running", f"PID={self._srda_pid}")
                        break
                except Exception:
                    pass
            if srda_running:
                self.log("SRDA 已在运行")
            if not srda_running and os.path.exists(SRDA_EXE):
                import subprocess
                import ctypes

                # 抓包需要管理员权限，和 SRDC 一样处理
                if ctypes.windll.shell32.IsUserAnAdmin():
                    self.log("当前已是管理员权限，直接启动 SRDA...")
                    CREATE_NEW_PROCESS_GROUP = 0x00000200
                    self._process = subprocess.Popen(
                        [SRDA_EXE],
                        cwd=SRDA_DIR,
                        creationflags=CREATE_NEW_PROCESS_GROUP
                    )
                    self._started_by_us = True
                    self._srda_pid = self._process.pid
                    self.log(f"SRDA 已启动 (PID={self._srda_pid})，等待初始化...")
                    self._emit_srda_status("waiting", f"PID={self._srda_pid}")
                else:
                    # 非管理员：用 ShellExecuteW 提权启动
                    self.log("以管理员权限启动 SRDA（会弹出 UAC 提示）...")
                    ret = ctypes.windll.shell32.ShellExecuteW(
                        None, "runas", SRDA_EXE, None, SRDA_DIR, 1
                    )
                    if ret <= 32:
                        self.log(f"[警告] ShellExecute 返回 {ret}，启动可能失败")
                        self._emit_srda_status("error", "启动失败")
                    else:
                        self._started_by_us = True
                        self.log("SRDA 启动请求已发送，请允许 UAC 提示")
                        self._emit_srda_status("waiting", "UAC 提权中")

                # 等待 BattleHistory 目录出现
                wait_count = 0
                while not os.path.isdir(self.battle_dir) and wait_count < 30:
                    time.sleep(1)
                    wait_count += 1
                if os.path.isdir(self.battle_dir):
                    self.log("SRDA BattleHistory 目录已就绪")
                else:
                    self.log("[警告] 等待 BattleHistory 目录超时")
        except Exception as e:
            self.log(f"启动 SRDA 失败: {e}")

        while self.running:
            try:
                self._check_latest()
                # 每 5 次轮询检查一次 SRDA 进程状态
                self._status_check_counter += 1
                if self._status_check_counter >= 5:
                    self._status_check_counter = 0
                    self._check_srda_process()
            except Exception as e:
                self.log(f"读取异常: {e}")
            time.sleep(self.poll_interval)

        if self.on_status:
            self.on_status("stopped")
        self._emit_srda_status("stopped")
        self.log("BattleHistory 监控停止")

    def stop(self, force_stop_srda: bool = False):
        self.running = False

        if not self._started_by_us and not force_stop_srda:
            return

        # 停止我们启动的 SRDA 进程
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
            except Exception:
                pass

        if self._started_by_us or force_stop_srda:
            try:
                import psutil
                for proc in psutil.process_iter(['name', 'pid']):
                    try:
                        name = proc.info['name'] or ''
                        if 'StarResonanceDpsAnalysis' in name and 'WPF' in name:
                            try:
                                p = psutil.Process(proc.info['pid'])
                                p.terminate()
                                try:
                                    p.wait(timeout=3)
                                except psutil.TimeoutExpired:
                                    p.kill()
                                self.log(f"已停止 SRDA 进程 (PID={proc.info['pid']})")
                            except Exception as e:
                                self.log(f"停止 SRDA 进程 (PID={proc.info['pid']}) 失败: {e}")
                    except Exception:
                        pass
            except Exception:
                pass

    def _emit_srda_status(self, status: str, detail: str = ""):
        """发送 SRDA 状态回调"""
        if self.on_srda_status:
            try:
                self.on_srda_status(status, detail)
            except Exception:
                pass

    def _check_srda_process(self):
        """定期检查 SRDA 进程是否仍在运行"""
        if not self._srda_pid:
            return
        try:
            import psutil
            try:
                p = psutil.Process(self._srda_pid)
                if p.is_running():
                    # 进程在运行，根据数据更新情况显示状态
                    if self._last_data_time:
                        from datetime import datetime, timedelta
                        elapsed = (datetime.now() - self._last_data_time).total_seconds()
                        if elapsed < 30:
                            self._emit_srda_status("running", f"PID={self._srda_pid}")
                        else:
                            self._emit_srda_status("waiting", f"PID={self._srda_pid} 无新数据{int(elapsed)}s")
                    else:
                        self._emit_srda_status("waiting", f"PID={self._srda_pid} 等待战斗")
            except psutil.NoSuchProcess:
                self._srda_pid = None
                self._emit_srda_status("error", "进程已退出")
                self.log("[警告] SRDA 进程已退出!")
        except Exception:
            pass

    def _send_default_state(self):
        """发送默认状态：1血/kamuXiY（用于无新数据时的测试）"""
        default_player = PlayerHealthInfo(
            name="kamuXiY",
            uid=0,
            current_hp=1,
            max_hp=1,
            health_percent=100.0,
            level=0,
            is_self=True
        )
        self._last_hp = 1
        self.log("使用默认状态: kamuXiY HP=1/1 (100.0%) [等待新战斗数据]")
        if self.on_state:
            try:
                self.on_state(default_player)
            except Exception:
                pass

    def _check_latest(self):
        """检查最新的 BattleHistory JSON 文件"""
        import json
        import glob

        # 查找最新的 Current_*.json（当前战斗数据，比 Total 更频繁更新）
        pattern = os.path.join(self.battle_dir, "Current_*.json")
        files = glob.glob(pattern)
        if not files:
            # 也检查 Total_*.json
            pattern = os.path.join(self.battle_dir, "Total_*.json")
            files = glob.glob(pattern)
        if not files:
            # 没有文件，发送默认状态（只发一次）
            if not self._default_sent:
                self._default_sent = True
                self._send_default_state()
            return

        # 按修改时间排序，取最新
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        latest = files[0]
        mtime = os.path.getmtime(latest)

        # 首次检查时记录文件时间信息，便于诊断
        if self._last_file is None:
            file_time = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            start_time = datetime.fromtimestamp(self._start_time).strftime("%Y-%m-%d %H:%M:%S")
            is_new = mtime >= self._start_time
            self.log(f"最新文件: {os.path.basename(latest)} (修改: {file_time}, 启动: {start_time}, {'新' if is_new else '旧'})")

        # 如果文件没变化，跳过
        if latest == self._last_file and mtime == self._last_mtime:
            return

        self._last_file = latest
        self._last_mtime = mtime

        # 过滤旧数据：如果文件修改时间在启动前，发送默认状态
        is_new_data = self._start_time and mtime >= self._start_time
        if is_new_data:
            self._last_data_time = datetime.now()
            self._default_sent = False  # 有新数据了，清除默认标志
        else:
            # 旧数据，发送默认1血/kamuXiY状态（只发一次）
            if not self._default_sent:
                self._default_sent = True
                self._send_default_state()
            return

        # 读取 JSON
        try:
            with open(latest, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"解析 JSON 失败: {e}")
            return

        # 提取玩家血量
        players = data.get("Players", {})
        if not players:
            return

        # 找到自身玩家（优先用 appsettings.json 中的 UID）
        self_player = None
        all_players = []
        for uid_str, info in players.items():
            uid = int(uid_str)
            hp = info.get("HP", 0)
            max_hp = info.get("MaxHP", 0)
            name = info.get("Name", "")
            level = info.get("Level", 0)

            phi = PlayerHealthInfo(
                name=name,
                uid=uid,
                current_hp=hp,
                max_hp=max_hp,
                health_percent=(hp / max_hp * 100) if max_hp > 0 else 0,
                level=level,
                is_self=False
            )
            all_players.append(phi)

            # 自身玩家：UID 匹配 appsettings.json
            if self._self_uid and uid == self._self_uid:
                self_player = phi
            elif not self._self_uid and name and info.get("ProfessionID", 0) > 0:
                # 没有 UID 配置时，选第一个有名字的玩家
                if self_player is None:
                    self_player = phi

        if not all_players:
            return

        if not self_player:
            self_player = all_players[0]
        self_player.is_self = True

        # 只在血量变化时输出
        if self_player.current_hp != self._last_hp:
            self._last_hp = self_player.current_hp
            self.log(
                f"血量更新: {self_player.name} "
                f"HP={self_player.current_hp:,}/{self_player.max_hp:,} "
                f"({self_player.health_percent:.1f}%) "
                f"[来自: {os.path.basename(latest)}]"
            )
            if self.on_state:
                try:
                    self.on_state(self_player)
                except Exception:
                    pass

    def get_stats(self) -> Dict:
        return {
            "packets": 0,
            "bytes": 0,
            "messages": 0,
            "flows": 0,
            "opcode_count": 0,
            "hp": self._last_hp,
            "max_hp": 0,
            "hp_percent": 0,
            "player_name": "",
        }


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def list_interfaces() -> List[Tuple[str, str]]:
    if not SCAPY_AVAILABLE:
        return []
    ifs = get_if_list()
    return [(name, name) for name in ifs]


@dataclass
class ParsedGameMessage:
    """解析后的游戏消息"""
    direction: str  # "S->C" or "C->S"
    total_len: int
    opcode: int
    flags: int = 0
    req_id: int = 0
    ack_id: int = 0
    channel: int = 0
    seq: int = 0
    proto_payload: bytes = b""
    proto_msg: Optional[ParsedMessage] = None
    zstd_decompressed: bool = False


class GameProtocolParser:
    """游戏协议解析器 - 处理分帧、头部解析、protobuf解析"""

    HEADER_SIZE = 20  # opcode之后的20字节头部
    MIN_MSG_SIZE = 6  # 4B len + 2B opcode

    @staticmethod
    def parse_stream(data: bytes, direction: str) -> Tuple[List[ParsedGameMessage], bytes]:
        """
        从字节流中解析完整消息，返回 (消息列表, 剩余不完整数据)
        """
        messages = []
        offset = 0

        while len(data) - offset >= GameProtocolParser.MIN_MSG_SIZE:
            total_len = struct.unpack(">I", data[offset:offset + 4])[0]

            if total_len < 2 or total_len > 10_000_000:
                break

            msg_end = offset + 4 + total_len
            if msg_end > len(data):
                break

            opcode = struct.unpack(">H", data[offset + 4:offset + 6])[0]
            payload = data[offset + 6:msg_end]

            msg = ParsedGameMessage(
                direction=direction,
                total_len=total_len,
                opcode=opcode,
            )

            # 解析20字节头部
            if len(payload) >= GameProtocolParser.HEADER_SIZE:
                msg.flags = struct.unpack(">I", payload[0:4])[0]
                msg.req_id = struct.unpack(">I", payload[4:8])[0]
                msg.ack_id = struct.unpack(">I", payload[8:12])[0]
                msg.channel = struct.unpack(">I", payload[12:16])[0]
                msg.seq = struct.unpack(">I", payload[16:20])[0]
                msg.proto_payload = payload[GameProtocolParser.HEADER_SIZE:]
            else:
                msg.proto_payload = payload

            # 尝试protobuf解析
            if msg.proto_payload:
                # 先尝试zstd解压
                decompressed = try_zstd_decompress(msg.proto_payload)
                if decompressed and len(decompressed) > len(msg.proto_payload) * 0.5:
                    msg.proto_payload = decompressed
                    msg.zstd_decompressed = True

                try:
                    proto_msg = parse_protobuf(msg.proto_payload)
                    if proto_msg.fields:
                        msg.proto_msg = proto_msg
                except Exception:
                    pass

            messages.append(msg)
            offset = msg_end

        remaining = data[offset:]
        return messages, remaining


@dataclass
class PlayerHealthInfo:
    """玩家血量信息"""
    name: str = ""
    uid: int = 0
    current_hp: int = 0
    max_hp: int = 0
    health_percent: float = 0.0
    level: int = 0
    is_self: bool = False
    profession: str = ""


class PacketCaptureWorker(threading.Thread):
    """
    抓包工作线程 - 替代 OCRWorker。

    回调:
    - on_state: 血量状态更新 (PlayerHealthInfo)
    - on_log: 日志输出
    - on_error: 错误信息
    - on_status: 状态变化 (running/stopped/no_permission)
    """

    def __init__(self, interface: Optional[str] = None,
                 on_state: Optional[Callable] = None,
                 on_log: Optional[Callable] = None,
                 on_error: Optional[Callable] = None,
                 on_status: Optional[Callable] = None):
        super().__init__(daemon=True)
        self.interface = interface
        self.on_state = on_state
        self.on_log = on_log
        self.on_error = on_error
        self.on_status = on_status

        self.running = False
        self._stream_buffers: Dict[str, bytes] = defaultdict(bytes)

        # 统计
        self.pkt_count = 0
        self.byte_count = 0
        self.msg_count = 0
        self.opcode_stats: Dict[int, int] = defaultdict(int)
        self.channel_stats: Dict[int, int] = defaultdict(int)
        self.flow_stats: Dict[Tuple, Dict] = defaultdict(lambda: {"packets": 0, "bytes": 0})

        # 血量数据
        self.self_health = PlayerHealthInfo(is_self=True)
        self.players: Dict[int, PlayerHealthInfo] = {}

        # HP字段探测: 记录所有可能的HP值及其出现频率
        self._hp_candidate_log: List[Tuple[int, int, str]] = []  # (value, field_num, path)

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        full_msg = f"[抓包 {ts}] {msg}"
        print(full_msg, flush=True)
        if self.on_log:
            try:
                self.on_log(full_msg)
            except Exception:
                pass

    def run(self):
        if not SCAPY_AVAILABLE:
            self.log("[错误] scapy 未安装，请运行: pip install scapy")
            if self.on_error:
                self.on_error("scapy 未安装")
            return

        if not is_admin():
            self.log("[错误] 需要管理员权限运行抓包!")
            self.log("  请以管理员身份打开终端再启动程序")
            if self.on_status:
                self.on_status("no_permission")
            return

        self.running = True
        if self.on_status:
            self.on_status("running")

        self.log(f"抓包线程启动，目标={TARGET_IP}")
        self.log(f"网卡: {self.interface or '自动选择'}")

        bpf = f"tcp and host {TARGET_IP}"
        self.log(f"BPF过滤器: {bpf}")

        try:
            while self.running:
                kwargs = {
                    "filter": bpf,
                    "prn": self._on_packet,
                    "timeout": 2,
                    "store": False,
                }
                if self.interface:
                    kwargs["iface"] = self.interface
                sniff(**kwargs)
        except Exception as e:
            self.log(f"抓包异常: {e}")
            traceback.print_exc()
            if self.on_error:
                self.on_error(str(e))
        finally:
            if self.on_status:
                self.on_status("stopped")
            self.log("抓包线程结束")

    def stop(self):
        self.running = False

    def _on_packet(self, pkt):
        if not self.running:
            return
        if not pkt.haslayer(IP) or not pkt.haslayer(TCP) or not pkt.haslayer(Raw):
            return

        ip = pkt[IP]
        tcp = pkt[TCP]
        if ip.src != TARGET_IP and ip.dst != TARGET_IP:
            return

        payload = bytes(pkt[Raw].load)
        if not payload:
            return

        direction = "S->C" if ip.src == TARGET_IP else "C->S"
        self.pkt_count += 1
        self.byte_count += len(payload)

        key = (ip.src, tcp.sport, ip.dst, tcp.dport)
        self.flow_stats[key]["packets"] += 1
        self.flow_stats[key]["bytes"] += len(payload)

        # 添加到流缓冲并解析
        buf = self._stream_buffers[direction] + payload
        messages, remaining = GameProtocolParser.parse_stream(buf, direction)
        self._stream_buffers[direction] = remaining

        if messages:
            self.msg_count += len(messages)
            for msg in messages:
                self.opcode_stats[msg.opcode] += 1
                self.channel_stats[msg.channel] += 1
                self._process_message(msg)

        # 每100个包输出一次统计
        if self.pkt_count % 200 == 0:
            self._log_stats()

    def _process_message(self, msg: ParsedGameMessage):
        """处理单条解析后的消息，尝试提取血量"""
        if not msg.proto_msg:
            return

        # 只处理服务器下发的消息 (S->C) 中的响应
        if msg.direction != "S->C" or msg.opcode != 2:
            return

        # 查找HP候选值
        hp_candidates = find_hp_candidates(msg.proto_msg)
        if hp_candidates:
            for c in hp_candidates[:5]:
                self._hp_candidate_log.append((c["value"], c["field"], c["path"]))

            # 尝试识别自身血量 (找最大值，且成对出现hp==maxhp)
            values = [c["value"] for c in hp_candidates]
            if values:
                max_val = max(values)
                # 如果最大值出现至少两次，可能是 HP 和 MaxHP
                if values.count(max_val) >= 2 and max_val > 1000:
                    if max_val != self.self_health.max_hp:
                        self.self_health.max_hp = max_val
                        self.self_health.current_hp = max_val
                        self.self_health.health_percent = 100.0
                        self.log(f"[血量探测] 疑似 MaxHP = {max_val:,}")
                        self._emit_state()

    def _emit_state(self):
        """发出状态更新"""
        if self.on_state and self.self_health.max_hp > 0:
            try:
                self.on_state(self.self_health)
            except Exception:
                pass

    def _log_stats(self):
        """输出统计信息"""
        flows = len(self.flow_stats)
        self.log(
            f"统计: {self.pkt_count}包 / {self.byte_count:,}字节 / "
            f"{self.msg_count}消息 / {flows}流 / "
            f"opcode数={len(self.opcode_stats)}"
        )

    def get_stats(self) -> Dict:
        return {
            "packets": self.pkt_count,
            "bytes": self.byte_count,
            "messages": self.msg_count,
            "flows": len(self.flow_stats),
            "opcode_count": len(self.opcode_stats),
            "hp": self.self_health.current_hp,
            "max_hp": self.self_health.max_hp,
            "hp_percent": self.self_health.health_percent,
            "player_name": self.self_health.name,
        }


if __name__ == "__main__":
    print("=" * 60)
    print("抓包模块测试")
    print(f"目标IP: {TARGET_IP}")
    print(f"管理员权限: {is_admin()}")
    print(f"scapy可用: {SCAPY_AVAILABLE}")
    print("=" * 60)

    if not is_admin():
        print("\n[!] 需要管理员权限!")
        sys.exit(1)

    worker = PacketCaptureWorker(
        on_log=lambda msg: print(msg),
        on_error=lambda e: print(f"[ERROR] {e}"),
        on_status=lambda s: print(f"[STATUS] {s}"),
        on_state=lambda h: print(f"[HP] {h.current_hp}/{h.max_hp} ({h.health_percent:.1f}%)")
    )
    worker.start()

    try:
        while worker.running:
            time.sleep(5)
            stats = worker.get_stats()
            print(f"\n[统计] 包={stats['packets']} 字节={stats['bytes']:,} "
                  f"消息={stats['messages']} 流={stats['flows']}")
            if stats['max_hp'] > 0:
                print(f"[血量] {stats['hp']:,}/{stats['max_hp']:,} ({stats['hp_percent']:.1f}%)")
    except KeyboardInterrupt:
        print("\n停止抓包...")
        worker.stop()
        worker.join(timeout=3)


class SrdcApiReader(threading.Thread):
    """
    通过 StarResonanceDamageCounter (Node.js版) 的 HTTP API 获取实时战斗数据。

    接口: GET http://localhost:8989/api/data
    返回格式包含 user (玩家) 和 enemy (敌人) 数据，含血量、DPS、职业等信息。
    """

    def __init__(self,
                 api_url: Optional[str] = None,
                 on_state: Optional[Callable] = None,
                 on_log: Optional[Callable] = None,
                 on_error: Optional[Callable] = None,
                 on_status: Optional[Callable] = None,
                 on_srda_status: Optional[Callable] = None,
                 poll_interval: float = 0.5):
        super().__init__(daemon=True)
        self.api_url = api_url or SRDC_API_URL
        self.on_state = on_state
        self.on_log = on_log
        self.on_error = on_error
        self.on_status = on_status
        self.on_srda_status = on_srda_status
        self.poll_interval = poll_interval

        self.running = False
        self._process = None
        self._started_by_us = False
        self._last_hp = 0
        self._last_max_hp = 0
        self._player_name = ""
        self._self_uid = 0
        self._self_name = ""
        self._debug_printed = False
        self._last_players_hash = 0
        self._last_callback_time = 0
        self._min_callback_interval = 0.2
        self._srdc_pid = None
        self._current_device_idx = None
        self._current_device_desc = None
        self._data_received = False
        self._last_data_time = None

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        full_msg = f"[SRDC {ts}] {msg}"
        print(full_msg, flush=True)
        if self.on_log:
            try:
                self.on_log(full_msg)
            except Exception:
                pass

    def _emit_srda_status(self, status: str, detail: str = ""):
        """发送 SRDC 状态回调"""
        if self.on_srda_status:
            try:
                self.on_srda_status(status, detail)
            except Exception:
                pass

    def _format_device_str(self) -> str:
        """格式化当前网卡信息字符串"""
        if self._current_device_idx is not None and self._current_device_desc:
            # 截断过长的网卡描述
            desc = self._current_device_desc
            if len(desc) > 30:
                desc = desc[:30] + "..."
            return f"[{self._current_device_idx}] {desc}"
        return ""

    def _format_status_detail(self) -> str:
        """格式化状态栏附加信息：PID + 网卡"""
        parts = []
        if self._srdc_pid:
            parts.append(f"PID={self._srdc_pid}")
        dev = self._format_device_str()
        if dev:
            parts.append(dev)
        return " ".join(parts)

    def set_self_name(self, name: str):
        """设置自身玩家名称，用于匹配自身玩家"""
        self._self_name = name
        self.log(f"设置自身玩家名称: {name}")

    def restart_srdc(self):
        """重启 SRDC 进程，清空其内部缓存"""
        import psutil
        import time
        
        self.log("正在重启 SRDC 以清空数据缓存...")
        
        # 1. 停止所有 SRDC 进程
        stopped_any = False
        for proc in psutil.process_iter(['name', 'pid', 'cmdline']):
            try:
                name = proc.info['name'] or ''
                cmdline = proc.info['cmdline'] or []
                cmdline_str = ' '.join(cmdline).lower()
                is_srdc = False
                if 'star-resonance-damage-counter' in name.lower():
                    is_srdc = True
                elif 'node' in name.lower() and 'server.js' in cmdline_str and 'damage' in cmdline_str:
                    is_srdc = True
                if is_srdc:
                    try:
                        p = psutil.Process(proc.info['pid'])
                        p.terminate()
                        try:
                            p.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            p.kill()
                        stopped_any = True
                        self.log(f"已停止 SRDC 进程 (PID={proc.info['pid']})")
                    except Exception as e:
                        self.log(f"停止 SRDC 进程 (PID={proc.info['pid']}) 失败: {e}")
            except Exception:
                pass
        
        # 重置进程引用
        self._process = None
        self._started_by_us = False
        self._last_players_hash = 0
        self._last_hp = 0
        self._last_max_hp = 0
        
        # 等待1秒确保进程完全退出
        time.sleep(1)
        
        # 2. 重新启动 SRDC
        if stopped_any:
            self.log("正在重新启动 SRDC...")
            # _run_inner 中的启动逻辑会自动检查并启动 SRDC
            # 我们只需要重置标志，让下一次循环检测到 SRDC 没运行就会启动
            # 不过读取线程一直在跑，所以需要等它自己检测到
            self.log("SRDC 重启中，请稍候...")
        else:
            self.log("未找到运行中的 SRDC 进程")

    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            import traceback
            self.log(f"[严重错误] SRDC API 读取器线程异常退出: {e}")
            self.log(traceback.format_exc())
            if self.on_error:
                try:
                    self.on_error(str(e))
                except Exception:
                    pass
        finally:
            self.running = False
            if self.on_status:
                try:
                    self.on_status("stopped")
                except Exception:
                    pass

    def _run_inner(self):
        self.running = True
        if self.on_status:
            self.on_status("running")

        self.log(f"SRDC API 读取器启动: {self.api_url}")

        # 检查 SRDC 程序是否在运行
        import psutil
        srdc_running = False
        for proc in psutil.process_iter(['name', 'pid', 'cmdline']):
            try:
                name = proc.info['name'] or ''
                cmdline = proc.info['cmdline'] or []
                cmdline_str = ' '.join(cmdline).lower()
                if 'star-resonance-damage-counter' in name.lower():
                    srdc_running = True
                    self._srdc_pid = proc.info['pid']
                    self.log(f"检测到 SRDC 进程 (PID={self._srdc_pid})")
                    self._emit_srda_status("running", f"PID={self._srdc_pid}")
                    break
                if 'node' in name.lower() and 'server.js' in cmdline_str and 'damage' in cmdline_str:
                    srdc_running = True
                    self._srdc_pid = proc.info['pid']
                    self.log(f"检测到 SRDC 进程 (Node.js, PID={self._srdc_pid})")
                    self._emit_srda_status("running", f"PID={self._srdc_pid}")
                    break
            except Exception:
                pass

        if not srdc_running:
            self.log("未检测到 SRDC 进程，尝试以管理员权限启动...")
            self._emit_srda_status("waiting", "启动中")
            if os.path.exists(SRDC_SERVER_JS):
                # 1. 先用 auto 模式启动
                self._start_srdc("auto")
                # 2. 等待 HTTP 服务就绪
                self._wait_for_http(15)
                # 3. 检查是否有有效数据（等待 12 秒）
                if self.running and not self._check_api_has_data(12):
                    self.log("auto 模式未检测到游戏数据，开始逐个尝试网卡...")
                    self._try_all_devices()
                else:
                    # auto 模式成功检测到数据，更新状态
                    self._emit_srda_status("running", self._format_status_detail())
            else:
                self.log(f"[警告] 未找到 SRDC 程序: {SRDC_SERVER_JS}")
                self.log("请先启动 StarResonanceDamageCounter")
                self._emit_srda_status("error", "未找到SRDC程序")
        else:
            # SRDC 已在运行，直接进入主循环
            self._started_by_us = False

        # 主循环：轮询 API
        retry_count = 0
        while self.running:
            if self._process is not None and self._process.poll() is not None:
                self.log("SRDC 进程已退出")
                self._emit_srda_status("error", "SRDC进程已退出")
                break

            try:
                data = self._fetch_api()
                if data:
                    retry_count = 0
                    self._process_data(data)
                    # 首次收到数据时切换到"运行中"状态
                    if not self._data_received:
                        self._data_received = True
                        self._last_data_time = time.time()
                        self._emit_srda_status("running", self._format_status_detail())
                else:
                    retry_count += 1
                    if retry_count % 20 == 0:
                        self.log(f"API 无数据或连接失败 (重试 {retry_count} 次)")
                        # 长时间无数据，切换到"等待数据"状态
                        if self._data_received:
                            self._emit_srda_status("waiting", f"{self._format_status_detail()} 无新数据")
            except Exception as e:
                self.log(f"API 读取异常: {e}")

            time.sleep(self.poll_interval)

        self.log("SRDC API 读取器已停止")
        self._emit_srda_status("stopped")

    def _start_srdc(self, device_arg: str = "auto"):
        """启动 SRDC 进程

        Args:
            device_arg: 网卡参数 - "auto" 或数字编号字符串
        """
        import subprocess
        import ctypes
        import tempfile

        # 先停止已有的 SRDC 进程
        self._stop_srdc_process()

        # 记录当前网卡信息（用于状态栏显示）
        if device_arg == "auto":
            self._current_device_idx = "auto"
            self._current_device_desc = "自动选择"
        else:
            self._current_device_idx = device_arg
            # 尝试从网卡列表获取描述
            try:
                devices = self._get_device_list()
                for idx, desc in devices:
                    if str(idx) == device_arg:
                        self._current_device_desc = desc
                        break
            except Exception:
                self._current_device_desc = None

        env = os.environ.copy()
        env["NO_OPEN"] = "1"
        env["SRDC_NO_OPEN"] = "1"
        env["DISABLE_OPEN"] = "1"

        if ctypes.windll.shell32.IsUserAnAdmin():
            self.log(f"以管理员权限启动 SRDC (网卡={device_arg})...")
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            self._process = subprocess.Popen(
                ["node", SRDC_SERVER_JS, device_arg, "info"],
                cwd=SRDC_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=CREATE_NEW_PROCESS_GROUP,
                env=env
            )
            self._started_by_us = True
            self._srdc_pid = self._process.pid
            self.log(f"SRDC 已启动 (PID={self._srdc_pid}, 网卡={device_arg})")
            self._emit_srda_status("waiting", self._format_status_detail())

            def read_output():
                try:
                    for line in self._process.stdout:
                        line = line.strip()
                        if line:
                            self.log(f"[SRDC输出] {line}")
                except Exception:
                    pass

            import threading as th
            output_thread = th.Thread(target=read_output, daemon=True)
            output_thread.start()
        else:
            # ShellExecuteW 提权时新进程继承 explorer 的环境，而非本进程环境
            # 用临时 launcher.bat 设置 NO_OPEN 后再启动 node，确保环境变量生效
            self.log(f"以管理员权限启动 SRDC (网卡={device_arg})，会弹出 UAC 提示...")
            launcher_path = os.path.join(tempfile.gettempdir(), "srdc_launcher.bat")
            with open(launcher_path, "w", encoding="ascii") as f:
                f.write("@echo off\r\n")
                f.write("set NO_OPEN=1\r\n")
                f.write("set SRDC_NO_OPEN=1\r\n")
                f.write("set DISABLE_OPEN=1\r\n")
                f.write(f'cd /d "{SRDC_DIR}"\r\n')
                f.write(f'start "" node "{SRDC_SERVER_JS}" {device_arg} info\r\n')

            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", launcher_path, None, SRDC_DIR, 0
            )
            if ret <= 32:
                self.log(f"[警告] ShellExecute 返回 {ret}，启动可能失败")
                self._emit_srda_status("error", "启动失败")
            else:
                self._started_by_us = True
                self.log(f"SRDC 启动请求已发送 (网卡={device_arg})，请允许 UAC 提示")
                self._emit_srda_status("waiting", f"UAC提权 {self._format_device_str()}")

    def _stop_srdc_process(self):
        """停止所有 SRDC 进程"""
        import psutil
        for proc in psutil.process_iter(['name', 'pid', 'cmdline']):
            try:
                name = proc.info['name'] or ''
                cmdline = proc.info['cmdline'] or []
                cmdline_str = ' '.join(cmdline).lower()
                is_srdc = False
                if 'star-resonance-damage-counter' in name.lower():
                    is_srdc = True
                elif 'node' in name.lower() and 'server.js' in cmdline_str and 'damage' in cmdline_str:
                    is_srdc = True
                if is_srdc:
                    try:
                        p = psutil.Process(proc.info['pid'])
                        p.terminate()
                        try:
                            p.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            p.kill()
                        self.log(f"已停止 SRDC 进程 (PID={proc.info['pid']})")
                    except Exception as e:
                        self.log(f"停止 SRDC 进程 (PID={proc.info['pid']}) 失败: {e}")
            except Exception:
                pass
        # 同时停止 self._process
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
            except Exception:
                pass
        self._process = None
        time.sleep(1)

    def _wait_for_http(self, timeout: int = 15):
        """等待 SRDC HTTP 服务就绪"""
        self.log(f"等待 SRDC HTTP 服务启动（最多 {timeout} 秒）...")
        for i in range(timeout * 2):
            if not self.running:
                return False
            time.sleep(0.5)
            if self._fetch_api() is not None:
                self.log("SRDC HTTP 服务已就绪!")
                return True
        self.log(f"[警告] SRDC HTTP 服务未在 {timeout} 秒内就绪")
        return False

    def _check_api_has_data(self, timeout: int = 12) -> bool:
        """检查 API 是否返回了有效的玩家数据

        Args:
            timeout: 等待超时秒数
        Returns:
            True 如果在超时内检测到有效玩家数据
        """
        self.log(f"检测 API 数据（等待 {timeout} 秒）...")
        for i in range(timeout * 2):
            if not self.running:
                return False
            time.sleep(0.5)
            data = self._fetch_api()
            if data and data.get("code") == 0:
                users = data.get("user", {})
                if users:
                    # 检查是否有玩家有有效数据（有名字或血量）
                    for uid, info in users.items():
                        name = info.get("name", "")
                        hp = info.get("hp", 0) or info.get("max_hp", 0)
                        if name or hp:
                            self.log(f"检测到有效玩家数据: {name or uid} (HP={hp})")
                            return True
        self.log("未检测到有效玩家数据")
        return False

    def _get_device_list(self) -> list:
        """获取网卡列表（通过 Node.js cap 库）

        Returns:
            [(index, description), ...] 网卡列表
        """
        import subprocess
        try:
            # 用 Node.js 内联脚本获取网卡列表
            script = (
                "const Cap=require('cap').Cap;"
                "Cap.deviceList().forEach((d,i)=>"
                "console.log(i+'|'+(d.description||d.name))"
                ");"
            )
            result = subprocess.run(
                ["node", "-e", script],
                cwd=SRDC_DIR,
                capture_output=True,
                text=True,
                timeout=5
            )
            devices = []
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if '|' in line:
                    parts = line.split('|', 1)
                    idx = int(parts[0])
                    desc = parts[1]
                    devices.append((idx, desc))
            return devices
        except Exception as e:
            self.log(f"获取网卡列表失败: {e}")
            return []

    def _is_virtual_adapter(self, desc: str) -> bool:
        """判断是否为虚拟网卡"""
        desc_lower = desc.lower()
        virtual_keywords = ['zerotier', 'vmware', 'hyper-v', 'virtual',
                           'loopback', 'tap', 'bluetooth', 'wan miniport',
                           'npcap', 'microsoft']
        return any(kw in desc_lower for kw in virtual_keywords)

    def _try_all_devices(self):
        """逐个尝试所有物理网卡，找到有游戏数据的网卡"""
        devices = self._get_device_list()
        if not devices:
            self.log("无法获取网卡列表，跳过网卡遍历")
            return

        # 过滤掉虚拟网卡，优先尝试物理网卡
        physical = [(i, d) for i, d in devices if not self._is_virtual_adapter(d)]
        virtual = [(i, d) for i, d in devices if self._is_virtual_adapter(d)]

        self.log(f"共 {len(devices)} 个网卡 (物理 {len(physical)}, 虚拟 {len(virtual)})")
        for i, d in physical:
            self.log(f"  [{i}] {d}")

        # 逐个尝试物理网卡
        for idx, desc in physical:
            if not self.running:
                return
            self.log(f"尝试网卡 [{idx}] {desc} ...")
            self._start_srdc(str(idx))
            if not self._wait_for_http(10):
                self.log(f"网卡 [{idx}] HTTP 服务未就绪，跳过")
                continue
            if self._check_api_has_data(8):
                self.log(f"找到有效网卡! [{idx}] {desc}")
                self._current_device_idx = idx
                self._current_device_desc = desc
                self._emit_srda_status("running", self._format_status_detail())
                return
            self.log(f"网卡 [{idx}] 无游戏数据")

        # 物理网卡都没有数据，尝试虚拟网卡
        for idx, desc in virtual:
            if not self.running:
                return
            self.log(f"尝试虚拟网卡 [{idx}] {desc} ...")
            self._start_srdc(str(idx))
            if not self._wait_for_http(10):
                continue
            if self._check_api_has_data(8):
                self.log(f"找到有效网卡! [{idx}] {desc}")
                self._current_device_idx = idx
                self._current_device_desc = desc
                self._emit_srda_status("running", self._format_status_detail())
                return
            self.log(f"网卡 [{idx}] 无游戏数据")

        self.log("所有网卡均已尝试，未找到游戏数据")
        self.log("请确认游戏正在运行且有网络流量")
        self._emit_srda_status("error", "未找到游戏数据网卡")

    def stop(self, force_stop_srdc: bool = False):
        """停止读取器和 SRDC 进程

        Args:
            force_stop_srdc: 是否强制停止 SRDC 进程（即使不是我们启动的）
        """
        self.running = False

        should_stop_srdc = self._started_by_us or force_stop_srdc

        if not should_stop_srdc:
            # 不停止 SRDC 进程，只停止读取线程和我们自己的子进程
            if self._process and self._process.poll() is None:
                try:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=3)
                    except Exception:
                        try:
                            self._process.kill()
                        except Exception:
                            pass
                except Exception:
                    pass
            return

        # 需要停止 SRDC 进程
        try:
            import psutil
            stopped_any = False
            for proc in psutil.process_iter(['name', 'pid', 'cmdline']):
                try:
                    name = proc.info['name'] or ''
                    cmdline = proc.info['cmdline'] or []
                    cmdline_str = ' '.join(cmdline).lower()
                    is_srdc = False
                    if 'star-resonance-damage-counter' in name.lower():
                        is_srdc = True
                    elif 'node' in name.lower() and 'server.js' in cmdline_str and 'damage' in cmdline_str:
                        is_srdc = True
                    if is_srdc:
                        try:
                            p = psutil.Process(proc.info['pid'])
                            p.terminate()
                            try:
                                p.wait(timeout=3)
                            except psutil.TimeoutExpired:
                                p.kill()
                            stopped_any = True
                            self.log(f"已停止 SRDC 进程 (PID={proc.info['pid']})")
                        except Exception as e:
                            self.log(f"停止 SRDC 进程 (PID={proc.info['pid']}) 失败: {e}")
                except Exception:
                    pass
            if not stopped_any:
                self.log("未找到需要停止的 SRDC 进程")
            else:
                self.log("所有 SRDC 进程已停止，缓存已清空")
            # 同时停止 self._process（如果有的话）
            if self._process and self._process.poll() is None:
                try:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=3)
                    except Exception:
                        try:
                            self._process.kill()
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            self.log(f"停止 SRDC 进程失败: {e}")

    def _fetch_api(self) -> Optional[Dict]:
        """调用 API 获取数据"""
        import urllib.request
        import json

        try:
            req = urllib.request.Request(self.api_url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode('utf-8'))
                    return data
        except Exception:
            pass
        return None

    def _process_data(self, data: Dict):
        """处理 API 返回的数据"""
        if data.get("code") != 0:
            return

        # 第一次获取到数据时，打印完整 JSON 结构以便调试
        if not self._debug_printed:
            self._debug_printed = True
            import json as _json
            try:
                pretty = _json.dumps(data, ensure_ascii=False, indent=2)
                self.log(f"API 返回数据（首次）:\n{pretty}")
            except Exception as e:
                self.log(f"打印 API 数据失败: {e}")

            # 单独打印第一个用户的完整信息
            users = data.get("user", {})
            if users:
                first_uid = next(iter(users))
                first_info = users[first_uid]
                try:
                    user_pretty = _json.dumps({first_uid: first_info}, ensure_ascii=False, indent=2)
                    self.log(f"第一个玩家完整信息:\n{user_pretty}")
                except Exception as e:
                    self.log(f"打印玩家信息失败: {e}")

        users = data.get("user", {})
        enemies = data.get("enemy", {})

        # 处理玩家数据
        # API 文档中 user 字段: realtime_dps, total_dps, total_damage, total_count,
        #   realtime_hps, total_hps, total_healing, taken_damage, profession
        # V3.3.6 可能新增了 hp/max_hp/name/fightPoint 等字段
        all_players = []
        self_player = None

        for uid_str, info in users.items():
            try:
                uid = int(uid_str)
            except ValueError:
                continue

            name = info.get("name", "")
            profession = info.get("profession", "")
            # 职业名称归一化
            PROFESSION_ALIASES = {
                "涤罪恶火·战斧": "赤炎狂战士",
            }
            for alias, standard in PROFESSION_ALIASES.items():
                if alias in profession:
                    profession = standard
                    break
            if not name:
                name = profession or f"玩家{uid}"

            hp = info.get("hp", 0)
            max_hp = info.get("max_hp", 0)

            # 如果 hp/max_hp 为 0，尝试从其他字段获取（如 fightPoint 等）
            # 这里保持 0 也没关系，UI 会显示"未检测"

            phi = PlayerHealthInfo(
                name=name,
                uid=uid,
                current_hp=hp,
                max_hp=max_hp,
                health_percent=(hp / max_hp * 100) if max_hp > 0 else 0,
                level=info.get("level", 0),
                is_self=False,
                profession=profession
            )
            all_players.append(phi)

        # 确定自身玩家：
        # 1. 如果设置了自身名称，优先按名称匹配
        # 2. 如果只有一个玩家，那就是自己
        # 3. 如果有多个，选 fightPoint 最高的或者第一个
        self_player = None
        if all_players:
            # 优先按名称匹配
            if self._self_name:
                for p in all_players:
                    if p.name == self._self_name:
                        self_player = p
                        break
            # 按 UID 匹配
            if self_player is None and self._self_uid:
                for p in all_players:
                    if p.uid == self._self_uid:
                        self_player = p
                        break
            #  fallback：只有一个玩家就是自己，否则选第一个
            if self_player is None:
                if len(all_players) == 1:
                    self_player = all_players[0]
                else:
                    self_player = all_players[0]
            self_player.is_self = True
            # 其余的作为队伍成员
            for p in all_players:
                if p is not self_player:
                    p.is_self = False

            # 重新排序：自身玩家放第一位
            others = [p for p in all_players if p is not self_player]
            all_players = [self_player] + others

            # 超过20人时，保留自身+其他19人（共20人）
            MAX_PLAYERS = 20
            if len(all_players) > MAX_PLAYERS:
                all_players = all_players[:MAX_PLAYERS]

        has_team = len(all_players) > 1

        # 每次都回调（不仅是血量变化时），确保 UI 能及时更新玩家列表
        self._last_hp = self_player.current_hp if self_player else 0
        self._last_max_hp = self_player.max_hp if self_player else 0
        self._player_name = self_player.name if self_player else ""

        # 如果有敌人数据，输出日志
        enemy_info = ""
        if enemies:
            for eid, einfo in enemies.items():
                ehp = einfo.get("hp", 0)
                emax_hp = einfo.get("max_hp", 0)
                ename = einfo.get("name", "")
                if emax_hp > 0:
                    enemy_info += f" | 敌人: {ename} HP={ehp:,}/{emax_hp:,}"

        if self_player:
            # 更新缓存值
            self._last_hp = self_player.current_hp
            self._last_max_hp = self_player.max_hp
            self._player_name = self_player.name

            # 计算玩家列表哈希（用于检测变化）
            current_hash = 0
            for p in all_players:
                current_hash = (current_hash * 31 + hash((p.name, p.current_hp, p.max_hp))) & 0xFFFFFFFF

            # 节流：避免过于频繁的回调
            import time as _time
            now = _time.time()

            is_first = (self._last_players_hash == 0 and current_hash != 0)
            changed = (current_hash != self._last_players_hash)
            time_ok = (now - self._last_callback_time) >= self._min_callback_interval

            if (is_first or changed) and time_ok:
                self._last_players_hash = current_hash
                self._last_callback_time = now

                self.log(
                    f"玩家: {self_player.name} "
                    f"HP={self_player.current_hp:,}/{self_player.max_hp:,} "
                    f"({self_player.health_percent:.1f}%)"
                    f" | 队伍: {len(all_players)}人"
                    f"{enemy_info}"
                )

                # 回调：传递 self_player, all_players, has_team
                if self.on_state:
                    try:
                        self.on_state(self_player, all_players, has_team)
                    except Exception:
                        # 兼容旧的单参数回调
                        try:
                            self.on_state(self_player)
                        except Exception:
                            pass

    def get_stats(self) -> Dict:
        return {
            "packets": 0,
            "bytes": 0,
            "messages": 0,
            "flows": 0,
            "opcode_count": 0,
            "hp": self._last_hp,
            "max_hp": self._last_max_hp,
            "hp_percent": (self._last_hp / self._last_max_hp * 100) if self._last_max_hp > 0 else 0,
            "player_name": self._player_name,
        }
