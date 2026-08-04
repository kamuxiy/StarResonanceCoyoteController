"""内置 DG-Lab WebSocket 服务端。

实现 DG-Lab Coyote 设备的 WebSocket 协议，让 DG-Lab 手机 APP 直连本程序。
协议与 CoyoteGameHub 完全兼容，APP 扫描二维码后通过 WebSocket 连接。

架构:
  DG-Lab APP(手机) ←WebSocket→ 本服务端 ←内部调用→ CoyoteDeviceManager → 设备控制

WebSocket 路径: /dglab_ws/{clientId}
端口: 与 HTTP 服务器共用 8920（通过 process_request 区分 HTTP/WS）

消息协议:
  1. 服务端 → APP: {type:"bind", clientId, targetId:"", message:"targetId"}
  2. APP → 服务端: {type:"bind", clientId, targetId, message:"DGLAB"}
  3. 服务端 → APP: {type:"bind", clientId, targetId, message:"200"}
  4. 心跳: 服务端 → APP: {type:"heartbeat", message:"200"}
  5. APP上报强度: {type:"msg", message:"strength-{A}+{B}+{limitA}+{limitB}"}
  6. 服务端设强度: {type:"msg", message:"strength-{channel}+{op}+{value}"} op:0=Sub,1=Add,2=Set
  7. 服务端发波形: {type:"msg", message:"pulse-{A|B}:{jsonArray}"}
  8. 服务端清波形: {type:"msg", message:"clear-{1|2}"} 1=A,2=B
  9. APP反馈: {type:"msg", message:"feedback-{button}"}
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Optional

import websockets
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

logger = logging.getLogger(__name__)

# ── 协议常量（与 CoyoteGameHub 源码完全一致）──
DGLAB_WS_PREFIX = "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"

MSG_HEARTBEAT = "heartbeat"
MSG_BIND = "bind"
MSG_MSG = "msg"
MSG_BREAK = "break"
MSG_ERROR = "error"

RC_SUCCESS = "200"
RC_CLIENT_DISCONNECTED = "209"
RC_INVALID_CLIENT_ID = "210"
RC_SERVER_DELAY = "211"
RC_ID_ALREADY_BOUND = "400"

DH_TARGET_ID = "targetId"
DH_DG_LAB = "DGLAB"
DH_STRENGTH = "strength"
DH_PULSE = "pulse"
DH_CLEAR = "clear"
DH_FEEDBACK = "feedback"

# 强度操作
OP_DECREASE = 0
OP_INCREASE = 1
OP_SET_TO = 2

# 通道
CH_A = 1
CH_B = 2

HEARTBEAT_INTERVAL = 10  # 秒
HEARTBEAT_TIMEOUT = 60   # 秒（DG-Lab APP 用户反应慢，延长到60秒）
WS_CLOSE_TIMEOUT = 5    # 秒（关闭握手等待）


def _to_ws_response(resp_tuple):
    """把 (status, headers, body) 元组转换为 websockets 16.x 的 Response 对象。

    coyote_http_server.handle_http_request 返回元组格式，但 websockets 16.x
    的 process_request 钩子要求返回 Response 对象。
    """
    status, headers_list, body = resp_tuple
    headers = Headers()
    for k, v in headers_list:
        headers[k] = v
    # 生成 reason phrase
    import http as _http
    try:
        reason = _http.HTTPStatus(status).phrase
    except ValueError:
        reason = "OK"
    return Response(status, reason, headers, body)


class DGLabWSClient:
    """一个通过 WebSocket 连接的 DG-Lab APP 客户端。"""

    def __init__(self, ws, client_id: str, manager: "CoyoteWsManager"):
        self.ws = ws
        self.client_id = client_id
        self.target_id = ""
        self.manager = manager
        self.active = False
        self.closed = False
        self.firing = False  # 是否正在执行开火（用于暂停空闲波形）

        # 设备上报的强度状态
        self.strength_a = 0
        self.limit_a = 200
        self.strength_b = 0
        self.limit_b = 200

        # 心跳任务
        self._heartbeat_task = None
        # 波形输出任务
        self._pulse_task = None
        self._pulse_stop = asyncio.Event()
        # 空闲波形循环任务
        self._idle_task = None
        self._idle_stop = asyncio.Event()

    # ── 空闲波形循环播放 ──
    async def _idle_wave_loop(self):
        """空闲波形循环：非 firing 状态下循环播放选定波形。"""
        self.manager._log_evt(f"[空闲波形] 客户端 {self.client_id[:8]}… 循环任务启动")
        pulse_idx = 0
        try:
            while not self._idle_stop.is_set() and not self.closed:
                if self.firing or not self.manager.idle_enabled:
                    # 开火中或未启用：等待，降低频率检查
                    try:
                        await asyncio.wait_for(self._idle_stop.wait(), timeout=0.2)
                    except asyncio.TimeoutError:
                        pass
                    continue

                try:
                    pulse_data = self.manager.idle_pulse_data
                    strength = self.manager.idle_strength
                    if not pulse_data:
                        await asyncio.sleep(0.2)
                        continue

                    # 夹强度到 APP limit
                    a_limit = self.limit_a or 200
                    capped_str = max(0, min(a_limit, strength))

                    # 每帧先设强度再发波形（一帧≈100ms）
                    if capped_str > 0:
                        await self.set_strength(CH_A, capped_str)
                    frame = pulse_data[pulse_idx % len(pulse_data)]
                    await self.send_pulse(CH_A, [frame])
                    pulse_idx += 1

                    # 每帧 100ms 节奏
                    try:
                        await asyncio.wait_for(self._idle_stop.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                except Exception as e:
                    logger.debug(f"[空闲波形] client={self.client_id[:8]}… 帧发送异常: {e}")
                    try:
                        await asyncio.wait_for(self._idle_stop.wait(), timeout=0.2)
                    except asyncio.TimeoutError:
                        pass
        except Exception as e:
            logger.warning(f"[空闲波形] 循环异常退出 client={self.client_id[:8]}… : {e}")
        finally:
            # 退出时把通道强度归零，避免残留
            try:
                await self.set_strength(CH_A, 0)
                await self.clear_pulse(CH_A)
            except Exception:
                pass
            self.manager._log_evt(f"[空闲波形] 客户端 {self.client_id[:8]}… 循环任务已停止")

    def start_idle_wave(self):
        """启动空闲波形循环（非阻塞，使用 manager 的 event loop）。"""
        if self._idle_task and not self._idle_task.done():
            return
        self._idle_stop.clear()
        loop = self.manager._loop
        if loop and loop.is_running():
            self._idle_task = asyncio.run_coroutine_threadsafe(
                self._idle_wave_loop(), loop)

    def stop_idle_wave(self):
        """停止空闲波形循环（非阻塞，UI 线程可安全调用）。

        只设置 Event 让协程在下一帧自检后退出，不等待 Future.result()，
        因为 result(timeout) 在 UI 线程会卡死事件循环直到超时。
        """
        self._idle_stop.set()
        # 取消仍在等待的 Future（非阻塞），实际退出交给 WS 事件循环
        if self._idle_task is not None:
            try:
                self._idle_task.cancel()
            except Exception:
                pass
        self._idle_task = None

    async def initialize(self):
        """初始化连接：发送 bind 请求并主动接收消息直到绑定成功。

        注意：必须在 initialize 中主动 recv，因为 _handle_connection 的消息循环
        （async for raw in ws）要等 initialize 返回后才会启动，如果 initialize
        只是 sleep 等待 target_id，会造成死锁——handle_message 永远不会被调用。
        """
        logger.info(
            f"[Bind] 开始绑定流程 client_id={self.client_id[:8]}… "
            f"remote={getattr(self.ws, 'remote_address', None)}")
        # 发送 bind 请求（APP 收到这个才会回复自己的 targetId）
        await self._send(MSG_BIND, DH_TARGET_ID)
        logger.info(f"[Bind] 已发送 bind 请求，等待 APP 回复 target_id…")

        # 主动接收消息直到绑定成功
        start = time.time()
        while not self.target_id:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                elapsed = time.time() - start
                if elapsed > HEARTBEAT_TIMEOUT:
                    logger.warning(
                        f"[Bind] 超时！ client={self.client_id[:8]}… "
                        f"{elapsed:.1f}s 未收到 APP 绑定回复 (等待>{HEARTBEAT_TIMEOUT}s)，"
                        f"即将断开")
                    try:
                        await self._send(MSG_BREAK, RC_SERVER_DELAY)
                        await asyncio.wait_for(self.ws.close(), timeout=WS_CLOSE_TIMEOUT)
                    except Exception:
                        pass
                    raise TimeoutError(f"Bind timeout ({elapsed:.1f}s)")
                if int(elapsed) % 5 == 0:
                    logger.debug(
                        f"[Bind] 等待 APP 回复… {elapsed:.1f}s "
                        f"(remote={getattr(self.ws, 'remote_address', None)})")
                continue
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
                mtype = msg.get("type", "")
                logger.debug(
                    f"[Bind] 收到消息: type={mtype!r} client={self.client_id[:8]}… "
                    f"msg(80)={str(msg)[:80]}")
                await self.handle_message(msg)
            except json.JSONDecodeError:
                logger.debug(f"[Bind] 非JSON消息: {raw[:80]!r}")
            except Exception as e:
                logger.warning(f"[Bind] initialize 处理消息异常: {e}", exc_info=True)

        # 绑定成功
        logger.info(
            f"[Bind] ✅ 绑定成功 client={self.client_id[:8]}… → "
            f"target={self.target_id[:8]}… 用时 {(time.time() - start):.1f}s")

        # 清除波形
        try:
            await self.clear_pulse(CH_A)
            await self.clear_pulse(CH_B)
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning(f"[Bind] 清除波形失败（可忽略）: {e}")

        # 启动心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.active = True
        logger.info(f"DG-Lab 客户端已绑定: {self.client_id} -> {self.target_id}")
        # 若全局已开启空闲波形播放：自动启动此客户端的空闲循环
        if self.manager.idle_enabled:
            self.start_idle_wave()

    async def _send(self, msg_type: str, message: str):
        """发送 WebSocket 消息。"""
        if self.closed:
            return
        data = json.dumps({
            "type": msg_type,
            "clientId": self.client_id,
            "targetId": self.target_id,
            "message": message,
        })
        try:
            await self.ws.send(data)
        except Exception as e:
            # 连接已关闭时的发送失败是正常现象，降级为 debug 日志
            if "1000" in str(e) or "CLOSED" in str(e).upper():
                logger.debug(f"WebSocket 已关闭，停止发送: {e}")
                self.closed = True
            else:
                logger.warning(f"WebSocket 发送失败: {e}")
            await self.close()

    async def _heartbeat_loop(self):
        """心跳循环。"""
        while self.active and not self.closed:
            try:
                await self._send(MSG_HEARTBEAT, RC_SUCCESS)
            except Exception:
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def handle_message(self, msg: dict):
        """处理收到的消息。"""
        mtype = msg.get("type", "")

        if mtype == MSG_BIND:
            if msg.get("message") == DH_DG_LAB:
                self.target_id = msg.get("targetId", "")
                await self._send(MSG_BIND, RC_SUCCESS)
                logger.info(f"DG-Lab 绑定成功: {self.client_id} -> {self.target_id}")

        elif mtype == MSG_MSG:
            message = msg.get("message", "")
            if message.startswith("feedback-"):
                btn = int(message.split("-")[1])
                logger.debug(f"DG-Lab 反馈按钮: {btn}")
            elif message.startswith("strength-"):
                # APP 上报强度变化: "strength-{A}+{B}+{limitA}+{limitB}"
                parts = message.split("-")[1].split("+")
                self.strength_a = int(parts[0])
                self.strength_b = int(parts[1])
                self.limit_a = int(parts[2])
                self.limit_b = int(parts[3])
                logger.debug(f"DG-Lab 强度上报: A={self.strength_a}/{self.limit_a} B={self.strength_b}/{self.limit_b}")
                # 通知管理器更新设备状态
                self.manager._on_strength_update(self)

        elif mtype == MSG_HEARTBEAT:
            pass  # 心跳响应

        elif mtype == MSG_BREAK:
            logger.info(f"DG-Lab 客户端断开: {self.client_id}")
            await self.close()

    # ── 控制指令 ──
    async def set_strength(self, channel: int, strength: int):
        """设置通道强度。channel: 1=A, 2=B。"""
        await self._send(MSG_MSG, f"{DH_STRENGTH}-{channel}+{OP_SET_TO}+{strength}")

    async def add_strength(self, channel: int, value: int):
        """增加通道强度。"""
        await self._send(MSG_MSG, f"{DH_STRENGTH}-{channel}+{OP_INCREASE}+{value}")

    async def sub_strength(self, channel: int, value: int):
        """减少通道强度。"""
        await self._send(MSG_MSG, f"{DH_STRENGTH}-{channel}+{OP_DECREASE}+{value}")

    async def send_pulse(self, channel: int, pulse_data: list):
        """发送波形数据到指定通道。channel: 1=A, 2=B。"""
        ch = "A" if channel == CH_A else "B"
        pulse_str = json.dumps(pulse_data)
        await self._send(MSG_MSG, f"{DH_PULSE}-{ch}:{pulse_str}")

    async def clear_pulse(self, channel: int):
        """清除通道波形。channel: 1=A, 2=B。"""
        ch = "1" if channel == CH_A else "2"
        await self._send(MSG_MSG, f"{DH_CLEAR}-{ch}")

    async def output_pulse(self, pulse_hex_list: list, channel: int,
                           duration_ms: int):
        """持续输出波形列表，直到时间结束。"""
        self._pulse_stop.clear()
        total = 0
        for hex_data in pulse_hex_list:
            if self._pulse_stop.is_set() or self.closed:
                break
            await self.send_pulse(channel, [hex_data])
            total += 100  # 每帧 100ms
            if total >= duration_ms:
                break
            try:
                await asyncio.wait_for(self._pulse_stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        """关闭连接。"""
        if self.closed:
            return
        self.active = False
        self.firing = False
        self._pulse_stop.set()
        # 停止空闲波形循环
        self._idle_stop.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        try:
            await self.set_strength(CH_A, 0)
            await self.clear_pulse(CH_A)
        except Exception:
            pass
        try:
            await self._send(MSG_BREAK, RC_CLIENT_DISCONNECTED)
        except Exception:
            pass
        try:
            await self.ws.close()
        except Exception:
            pass
        self.closed = True
        self.manager._on_client_close(self)


class CoyoteWsManager:
    """DG-Lab WebSocket 连接管理器。"""

    def __init__(self, device_manager):
        self._device_mgr = device_manager
        self._clients: dict[str, DGLabWSClient] = {}
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._loop = None
        self._running = False
        self._host = "0.0.0.0"
        self._port = 8920
        self._http_handler = None  # HTTP 请求处理器（回调）
        # 注意：回调属性必须用 _cb 后缀，避免与方法名 _on_client_connected 冲突
        # （__init__ 中的实例属性会覆盖类中同名方法，导致 None(client) 报错）
        self._on_client_connected_cb = None
        self._on_client_disconnected_cb = None
        self._log = []

        # ── 空闲波形播放配置 ──
        self.idle_enabled = False
        self.idle_strength = 20
        self.idle_pulse_id = "d6f83af0"   # 默认：呼吸
        self.idle_pulse_data = []         # 缓存的波形帧列表（hex string 数组）
        # 启动时先载入默认波形
        try:
            import coyote_device
            p = coyote_device.get_pulse_by_id(self.idle_pulse_id)
            if p:
                self.idle_pulse_data = list(p["pulseData"])
        except Exception:
            pass

    # ── 空闲波形配置接口（由 UI 调用）──
    def set_idle_wave_config(self, enabled: bool, strength: int, pulse_id: str):
        """设置空闲波形播放配置，自动对所有已连接客户端应用。"""
        self.idle_enabled = bool(enabled)
        self.idle_strength = max(1, min(200, int(strength)))
        loaded_name = ""
        if pulse_id and pulse_id != self.idle_pulse_id:
            self.idle_pulse_id = pulse_id
            # 重新加载波形数据
            try:
                import coyote_device
                p = coyote_device.get_pulse_by_id(pulse_id)
                if p:
                    self.idle_pulse_data = list(p["pulseData"])
                    loaded_name = p.get("name", pulse_id)
                    head_sample = ""
                    if self.idle_pulse_data:
                        s0 = self.idle_pulse_data[0]
                        head_sample = s0 if len(s0) <= 16 else s0[:16] + "…"
                    self._log_evt(
                        f"[空闲波形] 已加载波形: {loaded_name} "
                        f"(ID={pulse_id}, {len(self.idle_pulse_data)} 帧, 首帧={head_sample})")
                else:
                    self._log_evt(f"[空闲波形] 警告: 找不到波形 ID={pulse_id}，使用当前已有数据")
            except Exception as e:
                self._log_evt(f"[空闲波形] 加载波形失败: {e}")

        self._log_evt(
            f"[空闲波形] 配置已更新: 启用={self.idle_enabled} "
            f"强度={self.idle_strength} 波形ID={self.idle_pulse_id} "
            f"已缓存帧={len(self.idle_pulse_data)}")

        # 对所有已激活客户端应用
        with self._lock:
            clients = list(self._clients.values())
        started = 0
        stopped = 0
        for c in clients:
            if not c.active or c.closed:
                continue
            if self.idle_enabled:
                c.start_idle_wave()
                started += 1
            else:
                c.stop_idle_wave()
                stopped += 1
        if started or stopped:
            self._log_evt(
                f"[空闲波形] 已对客户端应用: 启动{started}个 / 停止{stopped}个 "
                f"(总共 {len(clients)} 个连接)")

    @property
    def running(self) -> bool:
        return self._running

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def set_http_handler(self, handler):
        """设置 HTTP 请求处理器（用于与 HTTP 共用端口）。

        handler 签名: handler(method: str, path: str, headers, body: bytes)
                      -> (status: int, headers: list[(k,v)], body: bytes) | None
        """
        self._http_handler = handler

    def set_callbacks(self, on_connected=None, on_disconnected=None):
        """注册客户端连接/断开回调（线程安全，从任意线程调用）。"""
        self._on_client_connected_cb = on_connected
        self._on_client_disconnected_cb = on_disconnected

    def _log_evt(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log.append((ts, msg))
        logger.info(f"[CoyoteWS] {msg}")
        if len(self._log) > 200:
            self._log = self._log[-200:]

    def _submit(self, coro, desc: str = ""):
        """把协程提交到 WS 事件循环执行，异常自动打日志。"""
        if not self._loop:
            self._log_evt(f"[ERROR] {desc}: 事件循环未启动（self._loop is None），指令丢弃")
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _cb(fut, _desc=desc):
            try:
                fut.result()
            except Exception as e:
                self._log_evt(f"[ERROR] {_desc}: 协程异常: {type(e).__name__}: {e}")
                logger.exception(f"WS 协程异常 ({_desc})")

        future.add_done_callback(_cb)
        return future

    @property
    def logs(self):
        return list(self._log)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def get_connect_urls(self) -> list:
        """获取 DG-Lab APP 可扫描的连接 URL 列表。

        核心策略（避免手机扫码连接超时的关键）：
          - 不用 hostname → getaddrinfo（Windows 下常返回虚拟网卡 IP，手机不可达）
          - 直接遍历本机所有网络接口的 AF_INET 单播地址
          - 按真实网卡（非 127./169.254./VMware/Hyper-V/VirtualBox 虚拟）优先排序
          - 每个地址生成的 URL 里 clientId 必须是可预测的占位符字符串（{clientId}），
            保证 APP 扫码后能正确替换成自己生成的 clientId。
        """
        import socket
        import ipaddress

        urls = []
        seen_ips = set()

        def _ip_is_truly_lan(ip_str: str) -> bool:
            """粗略判断是否为可达的局域网 IP（过滤虚拟/本地/链路本地）。"""
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return False
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                return False
            if not ip.is_private:
                # 公网IP一般做不到手机→电脑直连，除非用户显式配置，降级
                return False
            # Windows 常见虚拟网段特征
            if ip_str.startswith(("192.168.56.",   # VirtualBox
                                  "192.168.139.",  # VMware 默认
                                  "192.168.233.",  # VMware 另一段
                                  "172.17.",       # Docker
                                  "172.18.", "172.19.", "172.20.",
                                  "172.21.", "172.22.", "172.23.",
                                  "172.24.", "172.25.", "172.26.",
                                  "172.27.", "172.28.", "172.29.",
                                  "172.30.", "172.31.")):
                return False
            return True

        # 1) 优先：枚举所有 interface 的真实 IP（跨平台 ctypes / netifaces 兜底用 socket）
        try:
            import netifaces
            for iface in netifaces.interfaces():
                try:
                    addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                except Exception:
                    continue
                for a in addrs:
                    ip = a.get("addr")
                    if not ip or ip in seen_ips:
                        continue
                    seen_ips.add(ip)
                    score = 0
                    if _ip_is_truly_lan(ip):
                        score += 100
                    urls.append((score, ip))
        except ImportError:
            # 没装 netifaces 时退回到 socket.gethostbyname_ex
            try:
                hostname = socket.gethostname()
                _unused, _aliases, ip_list = socket.gethostbyname_ex(hostname)
                for ip in ip_list:
                    if ip in seen_ips:
                        continue
                    seen_ips.add(ip)
                    score = 0
                    if _ip_is_truly_lan(ip):
                        score += 100
                    urls.append((score, ip))
            except Exception:
                pass

        # 2) 再兜底：直接尝试 UDP connect 8.8.8.8 获取出口 IP（最准确的"手机能找到电脑"的IP）
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                if ip and ip not in seen_ips:
                    seen_ips.add(ip)
                    score = 150 if _ip_is_truly_lan(ip) else 50
                    urls.append((score, ip))
        except Exception:
            pass

        # 按分数高→低排序（真实LAN IP 排最前）
        urls.sort(key=lambda x: x[0], reverse=True)
        result = []
        for _score, ip in urls:
            result.append({
                "domain": ip,
                # APP 需要的 clientId 占位符必须是精确的 "{clientId}"
                # （URL # 片段里不会被 encode，所以直接写字面量即可）
                "connectUrl": (
                    f"{DGLAB_WS_PREFIX}ws://{ip}:{self._port}/dglab_ws/{{clientId}}"
                ),
            })

        # 确保至少有 127.0.0.1（仅本机调试）
        if not result:
            result.append({
                "domain": "127.0.0.1",
                "connectUrl": (
                    f"{DGLAB_WS_PREFIX}ws://127.0.0.1:{self._port}/dglab_ws/{{clientId}}"
                ),
            })

        logger.info(f"生成的可连接 LAN IP 列表（按可达性排序）: "
                    f"{[(r['domain'], '真实LAN' if _ip_is_truly_lan(r['domain']) else '其他') for r in result]}")
        return result

    def get_client(self, client_id: str) -> Optional[DGLabWSClient]:
        with self._lock:
            return self._clients.get(client_id)

    def list_clients(self) -> list:
        with self._lock:
            return [
                {
                    "clientId": c.client_id,
                    "targetId": c.target_id,
                    "active": c.active,
                    "strengthA": c.strength_a,
                    "limitA": c.limit_a,
                    "strengthB": c.strength_b,
                    "limitB": c.limit_b,
                }
                for c in self._clients.values()
            ]

    # ── 启动/停止 ──
    def start(self, host: str = "0.0.0.0", port: int = 8920) -> bool:
        if self._running:
            return True
        self._host = host
        self._port = port
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="CoyoteWsServer")
        self._thread.start()
        # 等待启动
        for _ in range(50):
            if self._running:
                return True
            time.sleep(0.1)
        return self._running

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None

    async def _shutdown(self):
        # 关闭所有客户端
        for c in list(self._clients.values()):
            await c.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _run_loop(self):
        """在独立线程中运行 asyncio 事件循环。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            logger.warning(f"WebSocket 服务异常: {e}")
        finally:
            self._running = False
            self._loop.close()

    async def _serve(self):
        """启动 WebSocket 服务（与 HTTP 共用端口）。"""
        try:
            self._server = await serve(
                self._handle_connection,
                self._host,
                self._port,
                process_request=self._process_request,
            )
            self._running = True

            # ── 启动时网络可达性自检：把所有诊断结果打日志，用户扫不上直接看日志定位 ──
            import socket
            diagnostics = [""]
            diagnostics.append("=" * 58)
            diagnostics.append("  WS 服务启动诊断（Coyote WebSocket Server）")
            diagnostics.append("=" * 58)
            diagnostics.append(f"  监听地址       : {self._host}:{self._port}")

            # 1) TCP 回环自检：用 Python socket 尝试连 127.0.0.1
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2)
                    s.connect(("127.0.0.1", self._port))
                diagnostics.append("  TCP 回环       : ✅ 127.0.0.1 可达")
            except Exception as e:
                diagnostics.append(f"  TCP 回环       : ❌ 127.0.0.1 连不上: {e}")

            # 2) 所有本机非虚拟 LAN IP 做一次 TCP 连接自测
            urls = self.get_connect_urls()
            diagnostics.append(f"  可用 LAN IP 数 : {len(urls)}")
            for u in urls[:5]:
                ip = u["domain"]
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2)
                        s.connect((ip, self._port))
                    diagnostics.append(f"    IP {ip:<15} ✅ 本机可连（不保证手机可连）")
                except Exception as e:
                    diagnostics.append(f"    IP {ip:<15} ⚠️  本机都连不上: {e}")

            # 3) Windows 网络位置（公用/专用）与网络发现状态提示（不改系统，只给建议）
            try:
                import ctypes
                try:
                    # Win32 API: GetAdaptersInfo / IP_ADAPTER_INFO 跳过；
                    # 这里直接读 winsat 或注册表代价高，只给出简明操作建议。
                    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
                except Exception:
                    is_admin = False
                diagnostics.append(f"  当前进程权限   : {'管理员' if is_admin else '普通用户'}")
                diagnostics.append("")
                diagnostics.append("  常见'扫码连不上'原因（按优先级）：")
                diagnostics.append("    1. 手机与电脑不在同一 WiFi/局域网")
                diagnostics.append("    2. Windows 把当前网络设为「公用」且「网络发现」关闭")
                diagnostics.append("       操作: 设置 → 网络和Internet → 属性 → 专用")
                diagnostics.append("    3. 路由器开启了「AP 隔离/客户端隔离」（酒店WiFi 常见）")
                diagnostics.append("    4. 360/火绒/Defender 网络保护 拦截外部入站")
            except Exception:
                pass
            diagnostics.append("=" * 58)
            diagnostics.append("")
            diag_text = "\n".join(diagnostics)
            logger.info(diag_text)
            self._log_evt("服务已启动，运行诊断:")
            # 面板日志里只保留关键字段，避免刷屏
            self._log_evt(
                f"  监听: {self._host}:{self._port}, "
                f"LAN IP 数:{len(urls)}, 回环:{'OK' if len(urls) else '?'}")

            self._log_evt(f"WebSocket 服务已启动: ws://{self._host}:{self._port}")
            logger.info(f"Coyote WebSocket 服务启动: ws://{self._host}:{self._port}")
            await asyncio.Event().wait()  # 永久等待
        except OSError as e:
            logger.warning(f"WebSocket 服务启动失败（端口占用？）: {e}")
            self._running = False

    def _process_request(self, conn, request):
        """区分 HTTP 和 WebSocket 请求（websockets 16.x API）。

        websockets 16.x 的 process_request 钩子签名：
          (ServerConnection, Request) -> Response | None

          - 返回 None  → 交给 WebSocket 握手
          - 返回 Response 对象 → 作为 HTTP 响应直接返回，不升级 WS

        注意：websockets 16.x 的 process_request 只能拿到 Request 对象，
        无法直接读取 body。所以这里只支持 GET 请求；POST 请求建议走独立 HTTP
        端口或由 UI 直接调用内置 manager（dglab_client.py 的内置直连模式）。
        """
        path = request.path

        # ── TCP 到达日志：用于区分「网络层根本连不上」和「连上了但 APP 层失败」
        # 用户反馈"扫码超时" 场景下，这是关键观测点：
        #   - 如果从未出现 [TCP↔] → 手机到电脑的 TCP 连接被拦截（防火墙/路由器/AP隔离/不在同WiFi）
        #   - 如果出现 [TCP↔] 但没后续 → TCP 已通，问题在 APP 绑定协议/版本差异
        try:
            remote_ip = "unknown"
            if conn is not None and hasattr(conn, 'remote_address'):
                ra = conn.remote_address
                if hasattr(ra, '__iter__') and not isinstance(ra, str):
                    remote_ip = ra[0]
                else:
                    remote_ip = str(ra)
            if path.startswith("/dglab_ws/") or path == "/" or path == "":
                logger.info(
                    f"[TCP↔] 请求到达 remote={remote_ip} method={request.method} "
                    f"path={path!r}")
                self._log_evt(
                    f"[TCP↔] {remote_ip} {request.method} "
                    f"{path[:80]}{'…' if len(path) > 80 else ''}")
        except Exception:
            pass

        # WebSocket 升级请求交给 websockets 处理
        if path.startswith("/dglab_ws/"):
            return None

        # HTTP 请求交给 HTTP 处理器（GET 类请求）
        if self._http_handler:
            try:
                result = self._http_handler("GET", path, request.headers, b"")
                if result is not None:
                    return _to_ws_response(result)
            except Exception as e:
                logger.warning(f"HTTP 处理异常: {e}")

        # 默认返回简单响应
        body = json.dumps({"status": 1, "code": "OK",
                           "message": "Coyote Game Hub (WebSocket mode)"}).encode()
        return _to_ws_response((200, [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Access-Control-Allow-Origin", "*"),
        ], body))

    async def _handle_connection(self, ws):
        """处理新的 WebSocket 连接。"""
        # 拿到客户端 IP（用于定位超时根因）
        remote = getattr(ws, 'remote_address', None)
        try:
            _raw_remote = ws.remote_address
            remote_ip = _raw_remote[0] if hasattr(_raw_remote, '__iter__') and not isinstance(_raw_remote, str) else str(_raw_remote)
        except Exception:
            remote_ip = str(remote) if remote else "unknown"

        # 从路径中提取 clientId
        path = ws.request.path if hasattr(ws, 'request') else ""
        # websockets 16.x 用 ws.request.path
        if not path:
            path = getattr(ws, 'path', '')

        logger.info(
            f"[WS] 新的连接进入 remote={remote_ip} path={path!r}")
        self._log_evt(f"[握手] 新连接: remote={remote_ip} path={path[:80]}" +
                      ("…" if len(path) > 80 else ""))

        # 解析 clientId（注意：可能包含 URL 编码字符，如 %7B=``，%7D=``）
        client_id_raw = ""
        if "/dglab_ws/" in path:
            client_id_raw = path.split("/dglab_ws/")[-1].split("?")[0].split("/")[0]

        # ── 关键兼容处理 ──
        # DG-Lab APP 在拼接 URL 时，可能先把整条 URL 做了 path URL-encode，
        # 导致原始占位符 `` 变成 ``，再去替换 `{clientId}` 子串就替换失败，
        # 最终 APP 会把 `` 原样当作 clientId 发过来。
        # 策略：1) 先 URL decode；2) 对 placeholder 值不立即拒绝，
        #      而是继续走到 initialize 发送 bind，APP 如果是正版/新版本，
        #      bind 阶段会回复 targetId，即便 clientId 是占位符也能用（我们不会把
        #      client_id 用作鉴权，只靠 targetId 对应真机）；
        #      如果 N 秒后仍未收到 bind 回复，再断开并打印诊断信息。
        from urllib.parse import unquote
        client_id = unquote(client_id_raw)
        placeholder_detected = bool(client_id) and any(
            client_id.startswith(p) for p in ("{", "%7B", "undefined", "null", "")
        ) or client_id_raw != client_id

        if placeholder_detected:
            logger.warning(
                f"[WS] client_id 疑似占位符（容忍，继续握手）: "
                f"raw={client_id_raw!r} → decoded={client_id!r} remote={remote_ip}")
            self._log_evt(
                f"[警告] client_id 为占位符，继续等 bind: {client_id_raw[:16]}…")

        if not client_id:
            try:
                await ws.send(json.dumps({
                    "type": MSG_ERROR, "clientId": "", "targetId": "",
                    "message": RC_INVALID_CLIENT_ID,
                }))
                await asyncio.wait_for(ws.close(), timeout=WS_CLOSE_TIMEOUT)
            except Exception:
                pass
            return

        # 检查重复
        with self._lock:
            if client_id in self._clients:
                logger.warning(
                    f"[WS] 拒绝重复 client_id={client_id[:8]}… remote={remote_ip}，"
                    f"已存在的连接会被保留")
                try:
                    await ws.send(json.dumps({
                        "type": MSG_ERROR, "clientId": client_id, "targetId": "",
                        "message": RC_ID_ALREADY_BOUND,
                    }))
                    await asyncio.wait_for(ws.close(), timeout=WS_CLOSE_TIMEOUT)
                except Exception:
                    pass
                return

        # 创建客户端
        client = DGLabWSClient(ws, client_id, self)
        with self._lock:
            self._clients[client_id] = client

        self._log_evt(f"客户端连接: {client_id[:8]}… (from {remote_ip})")
        logger.info(
            f"[WS] 已注册 client_id={client_id[:8]}… remote={remote_ip} "
            f"（当前已连接 {len(self._clients)} 个客户端）")

        try:
            await client.initialize()

            # 通知设备管理器：真实设备已连接
            self._on_client_connected(client)
            logger.info(
                f"[WS] ✅ 真机已就绪 client={client_id[:8]}… "
                f"target={client.target_id[:8] if client.target_id else ''}…")

            # 消息循环
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                    await client.handle_message(msg)
                except json.JSONDecodeError:
                    logger.debug(f"[WS] 非JSON消息: {str(raw)[:80]!r}")
                except Exception as e:
                    logger.warning(f"[WS] 处理消息异常: {e}", exc_info=True)

        except asyncio.TimeoutError as e:
            logger.warning(
                f"[WS] 连接超时断开 client={client_id[:8]}… remote={remote_ip} : {e}")
            self._log_evt(f"[超时] {client_id[:8]}…: {e}")
        except Exception as e:
            logger.warning(
                f"[WS] 连接异常 client={client_id[:8]}… remote={remote_ip} : {e}",
                exc_info=True)
        finally:
            try:
                await client.close()
            except Exception:
                pass
            with self._lock:
                popped = self._clients.pop(client_id, None)
                if popped is not None:
                    logger.info(
                        f"[WS] 已清理连接 client={client_id[:8]}… "
                        f"（剩余 {len(self._clients)} 个）")

    # ── 事件回调 ──
    def _on_client_connected(self, client: DGLabWSClient):
        """真实设备已连接，注册到设备管理器。"""
        self._log_evt(f"设备已绑定: {client.client_id[:8]}… → {client.target_id[:8]}…")
        if self._on_client_connected_cb:
            self._on_client_connected_cb(client)

    def _on_client_close(self, client: DGLabWSClient):
        """设备断开。"""
        self._log_evt(f"设备断开: {client.client_id[:8]}…")
        with self._lock:
            self._clients.pop(client.client_id, None)
        if self._on_client_disconnected_cb:
            self._on_client_disconnected_cb(client)

    def _on_strength_update(self, client: DGLabWSClient):
        """设备强度上报（显示改为 /前后一致：A/B 均按 当前值/上限值 显示）。"""
        self._log_evt(
            f"强度上报 A={client.strength_a}/{client.limit_a} "
            f"B={client.strength_b}/{client.limit_b}")

    # ── 线程安全的控制方法 ──
    def send_strength_async(self, client_id: str, channel: int, strength: int):
        """线程安全地异步设置强度。"""
        client = self.get_client(client_id)
        if not client:
            self._log_evt(f"[ERROR] send_strength_async: 客户端 {client_id[:8] if client_id else ''}… 不存在")
            return
        ch_name = "A" if channel == CH_A else "B"
        self._log_evt(f"[CMD] → 设置强度 {ch_name}={strength} 客户端={client_id[:8]}…")
        self._submit(
            client.set_strength(channel, strength),
            f"set_strength({ch_name}, {strength}) @{client_id[:8]}…")

    def send_pulse_async(self, client_id: str, channel: int, pulse_data: list):
        """线程安全地异步发送波形。"""
        client = self.get_client(client_id)
        if not client:
            self._log_evt(f"[ERROR] send_pulse_async: 客户端不存在")
            return
        ch_name = "A" if channel == CH_A else "B"
        self._submit(
            client.send_pulse(channel, pulse_data),
            f"send_pulse({ch_name}, {len(pulse_data) if pulse_data else 0} 帧) @{client_id[:8]}…")

    def clear_pulse_async(self, client_id: str, channel: int):
        """线程安全地异步清除波形。"""
        client = self.get_client(client_id)
        if not client:
            return
        self._submit(
            client.clear_pulse(channel),
            f"clear_pulse @{client_id[:8]}…")

    def fire_async(self, client_id: str, strength: int, time_ms: int,
                   pulse_hex_list: list = None):
        """线程安全地异步开火。strength 范围 0-200。"""
        client = self.get_client(client_id)
        if not client:
            self._log_evt(f"[ERROR] fire_async: 客户端 {client_id[:8] if client_id else ''}… 不存在，开火被丢弃！"
                          f"（当前已连接客户端: {list(self._clients.keys())}）")
            return

        self._log_evt(
            f"[CMD] → 开火 strength={strength}(0-200) time={time_ms}ms "
            f"pulse={'自定义' if pulse_hex_list else '默认(呼吸)'} "
            f"客户端={client_id[:8]}…  target={client.target_id[:8] if client.target_id else '未绑定'}…")

        async def _do_fire():
            # 记录 fire 前 idle 是否启用 + 启用时的强度
            restore_idle = self.idle_enabled

            pulse_hex = pulse_hex_list
            if not pulse_hex:
                import coyote_device
                # 优先使用页面唯一的波形下拉当前选择（current_pulse_id），不写死呼吸
                device_mgr = coyote_device.get_default_manager()
                prefer_id = getattr(device_mgr, "current_pulse_id", "d6f83af0")
                pulse_def = coyote_device.get_pulse_by_id(prefer_id)
                if not pulse_def:
                    pulse_def = coyote_device.get_pulse_by_id("d6f83af0")
                pulse_hex = pulse_def["pulseData"] if pulse_def else None
                self._log_evt(
                    f"  [波形来源] 使用默认开火波形: "
                    f"{pulse_def.get('name', prefer_id) if pulse_def else prefer_id}"
                    f"({prefer_id}) {'[回退]' if prefer_id != getattr(device_mgr, 'current_pulse_id', prefer_id) else ''}")
            if not pulse_hex:
                self._log_evt("[ERROR] fire_async: 找不到默认波形 (current_pulse_id 无效且呼吸回退也失败)")
                return

            # ── DG-Lab socket 设强度不生效的根因 & 修复 ──
            # 原因：APP 端有两道独立的"强度上限" clamp：
            #   1) 通道硬件 limit (limit_a / limit_b，强度滑条上方的"安全上限")
            #      由用户在 APP 里手动左右拉到最大值；
            #   2) 当前通道强度滑条 (strength_a / strength_b 的当前值)
            #      滑条本身的上限就是 (1) 里的 limit。
            # 如果 APP 里 limit_a=29，却想 set_strength(A, 50)，APP 会直接把
            # 50 clamp 到 ≤ limit_a（也就是 29 或更常用的 0），结果就是「实际上
            # 完全没输出」——这就是用户说的"socket触发点火后实际上无强度输出，
            # 需要APP上手动输入强度"的直接原因。
            #
            # 做法（在协议允许范围内尽可能逼近目标强度）：
            #   a. 如果 limit_a < strength，先 log 警告（无法通过 socket 改 limit，
            #      这是APP端用户设置的值）；
            #   b. 把 strength 夹到 [0, min(limit_a, limit_b if B开启 else 200)]；
            #   c. 再做 set_strength → 输出波形 → sleep → 恢复到 idle 强度（不是 0）
            import math

            def _ch_limit(channel: int) -> int:
                return client.limit_a if channel == CH_A else client.limit_b

            a_limit = _ch_limit(CH_A)
            b_limit = _ch_limit(CH_B)
            capped_a = max(0, min(a_limit, strength))
            use_b = bool(client.limit_b > 0)
            capped_b = max(0, min(b_limit, strength)) if use_b else 0

            if capped_a != strength or (use_b and capped_b != strength):
                self._log_evt(
                    f"[警告] APP limit 不足: 目标强度={strength}, "
                    f"A_limit={a_limit}, B_limit={b_limit}, "
                    f"实际使用强度 A={capped_a} B={capped_b}。"
                    f"请在 APP 内把通道上限手动拉到 >= {strength} 以获得完整体验。")
                logger.warning(
                    f"fire_async capped strength({strength}) → A={capped_a}/{a_limit} "
                    f"B={capped_b}/{b_limit}: 需要用户在APP手动提高limit。")

            self._log_evt(f"  [执行] 1) 设置强度 A={capped_a}" +
                          (f" B={capped_b}" if use_b else ""))
            await client.set_strength(CH_A, capped_a)
            if use_b:
                await client.set_strength(CH_B, capped_b)

            self._log_evt(f"  [执行] 2) 输出波形 {len(pulse_hex)} 帧，持续 ≈{time_ms}ms")
            await client.output_pulse(pulse_hex, CH_A, time_ms)

            self._log_evt(f"  [执行] 3) 等待 {time_ms / 1000:.2f}s 让波形播放完毕")
            await asyncio.sleep(time_ms / 1000)

            # ⚠️ 关键：fire 结束后不要把强度归零到 0，否则用户开启的空闲波形会被"掐断"
            # 若当前空闲波形启用：A 通道恢复到 idle_strength（夹 APP limit）
            # 否则：按原逻辑归零
            if restore_idle:
                idle_cap_a = max(0, min(a_limit, self.idle_strength))
                self._log_evt(
                    f"  [执行] 4) 空闲波形已开启，恢复 A={idle_cap_a}" +
                    (f" B=0" if use_b else ""))
                await client.set_strength(CH_A, idle_cap_a)
                if use_b:
                    await client.set_strength(CH_B, 0)
            else:
                self._log_evt(f"  [执行] 4) 关闭强度 A=0" +
                              (f" B=0" if use_b else ""))
                await client.set_strength(CH_A, 0)
                if use_b:
                    await client.set_strength(CH_B, 0)
            self._log_evt(f"  [完成] 开火流程结束")

        self._submit(_do_fire(), f"fire(s={strength}, t={time_ms}ms) @{client_id[:8]}…")


# 全局单例
_default_ws_manager = None


def get_default_ws_manager() -> CoyoteWsManager:
    global _default_ws_manager
    if _default_ws_manager is None:
        import coyote_device
        _default_ws_manager = CoyoteWsManager(
            coyote_device.get_default_manager())
    return _default_ws_manager
