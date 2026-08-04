"""内置 Coyote Game Hub HTTP 服务器（端口 8920）。

完整实现 DG-Lab-Coyote-Game-Hub v2 OpenAPI 规范：
  GET  /                                  - 健康检查
  GET  /api/server_info                   - 获取服务器信息（含 WebSocket 连接URL，供APP扫码）
  GET  /api/client/connect                - 申请新的 clientId
  GET  /api/v2/pulse_list                 - 获取服务器波形列表
  GET  /api/v2/game/{clientId}            - 获取游戏信息
  GET  /api/v2/game/{clientId}/pulse_list - 获取完整波形列表
  GET  /api/v2/game/{clientId}/strength   - 获取强度配置
  POST /api/v2/game/{clientId}/strength   - 设置强度（SetStrengthSet/Add/Sub）
  GET  /api/v2/game/{clientId}/pulse      - 获取当前波形
  POST /api/v2/game/{clientId}/pulse      - 设置波形
  POST /api/v2/game/{clientId}/action/fire - 一键开火

支持两种运行模式：
  1. 独立 HTTP 服务器（ThreadingHTTPServer 占用端口 8920）
  2. 与 WebSocket 服务器共用端口：通过 handle_http_request() 函数被 ws_server 调用

兼容现有 DGLabClient HTTP 请求格式。
"""
import json
import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import coyote_device

logger = logging.getLogger(__name__)

# 默认端口（与 DGLabGameController / CoyoteGameHub 一致）
DEFAULT_PORT = 8920

# DG-Lab APP 扫码识别前缀（与 DGLabGameController 完全一致）
DGLAB_WS_PREFIX = "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"


def get_local_ip_list() -> list:
    """获取本机所有 IPv4 地址（排除 127.x）。"""
    import socket
    ips = []
    seen = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip in seen:
                continue
            seen.add(ip)
            ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def build_connect_urls(port: int) -> list:
    """构建 DG-Lab APP 可扫描的连接 URL 列表。

    返回结构与 DGLabGameHub 的 /api/server_info 兼容：
      [{"domain": "192.168.1.10",
        "connectUrl": "https://...#DGLAB-SOCKET#ws://192.168.1.10:8920/dglab_ws/{clientId}"}, ...]
    """
    return [
        {
            "domain": ip,
            "connectUrl": f"{DGLAB_WS_PREFIX}ws://{ip}:{port}/dglab_ws/{{clientId}}",
        }
        for ip in get_local_ip_list()
    ]


def _parse_body(body: bytes, content_type: str) -> dict:
    """解析 POST Body（支持 JSON 或 x-www-form-urlencoded）。"""
    if not body:
        return {}
    content_type = (content_type or "").lower()
    # JSON
    if "json" in content_type:
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {}
    # Form
    data = body.decode("utf-8", errors="ignore")
    qs = parse_qs(data, keep_blank_values=True)
    result = {}
    for k, v in qs.items():
        if len(v) == 1:
            result[k] = v[0]
        else:
            result[k] = v
    # 展开嵌套，如 strength.add=1 -> {"strength": {"add": 1}}
    expanded = {}
    for k, v in result.items():
        if "." in k:
            parts = k.split(".", 1)
            expanded.setdefault(parts[0], {})
            try:
                expanded[parts[0]][parts[1]] = int(v) if str(v).lstrip("-").isdigit() else v
            except (TypeError, ValueError):
                expanded[parts[0]][parts[1]] = v
        else:
            try:
                expanded[k] = int(v) if str(v).lstrip("-").isdigit() else v
            except (TypeError, ValueError):
                expanded[k] = v
    return expanded


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: dict):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


class CoyoteApiHandler(BaseHTTPRequestHandler):
    """Coyote Game Hub v2 API 处理器。"""

    def log_message(self, fmt, *args):  # 静默默认日志
        logger.debug("HTTP " + fmt % args)

    # ── 路由 ──
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        # 健康检查
        if path in ("", "/", "/health", "/status"):
            return _json_response(self, 200, {
                "status": 1, "code": "OK",
                "message": "内置 Coyote Game Hub Server 运行中",
                "clients": len(self.server.manager.list_clients()),
            })

        # 服务器信息（含 WebSocket 连接URL，供 APP 扫码）
        if path == "/api/server_info":
            return self._resp_server_info()

        # 申请新 clientId（DG-Lab APP 连接前调用）
        if path == "/api/client/connect":
            return self._resp_client_connect()

        # 波形列表（公共）
        if path == "/api/v2/pulse_list":
            return self._resp_pulse_list(include_custom=True)

        # /api/v2/game/{clientId}/xxx
        prefix = "/api/v2/game/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            parts = rest.split("/", 1)
            cid = parts[0]
            sub = parts[1] if len(parts) > 1 else ""
            return self._handle_game_get(cid, sub)

        return _json_response(self, 404, {
            "status": 0, "code": "ERR::NOT_FOUND", "message": f"404 {path}"
        })

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        ct = self.headers.get("Content-Type", "")
        data = _parse_body(body, ct)

        prefix = "/api/v2/game/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            parts = rest.split("/", 1)
            cid = parts[0]
            sub = parts[1] if len(parts) > 1 else ""
            return self._handle_game_post(cid, sub, data)

        # 兼容老接口
        if path in ("/api/strength", "/strength", "/api/action"):
            # strength/add/pulse 兼容
            return self._compat_legacy(path, data)

        return _json_response(self, 404, {
            "status": 0, "code": "ERR::NOT_FOUND", "message": f"404 {path}"
        })

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── 辅助：获取 manager ──
    @property
    def mgr(self) -> "coyote_device.CoyoteDeviceManager":
        return self.server.manager

    # ── GET /api/v2/game/{cid}/{sub} ──
    def _handle_game_get(self, cid: str, sub: str):
        if sub == "":
            return self._resp_game_info(cid)
        if sub == "strength":
            return self._resp_strength_config(cid)
        if sub == "pulse":
            return self._resp_current_pulse(cid)
        if sub == "pulse_list":
            return self._resp_pulse_list(include_custom=True)
        return _json_response(self, 404, {
            "status": 0, "code": "ERR::NOT_FOUND", "message": f"404 /api/v2/game/{cid}/{sub}"
        })

    # ── POST /api/v2/game/{cid}/{sub} ──
    def _handle_game_post(self, cid: str, sub: str, data: dict):
        if sub == "strength":
            return self._post_set_strength(cid, data)
        if sub == "pulse":
            return self._post_set_pulse(cid, data)
        if sub == "action/fire":
            return self._post_fire(cid, data)
        return _json_response(self, 404, {
            "status": 0, "code": "ERR::NOT_FOUND", "message": f"404 POST /api/v2/game/{cid}/{sub}"
        })

    # ── 响应实现 ──
    def _resp_game_info(self, cid: str):
        c = self.mgr.get_client(cid)
        if c is None and cid != "all":
            return _json_response(self, 404, self._err_client(cid))
        c = c or next(iter(self.mgr.resolve_client_ids("all")), None)
        if c is None:
            return _json_response(self, 200, self._err_client(cid))
        return _json_response(self, 200, c.to_game_info())

    def _resp_strength_config(self, cid: str):
        ids = self.mgr.resolve_client_ids(cid)
        if not ids:
            return _json_response(self, 200, {
                "status": 1, "code": "OK",
                "strengthConfig": {"strength": 0, "randomStrength": 0}
            })
        c = self.mgr.get_client(ids[0])
        return _json_response(self, 200, {
            "status": 1, "code": "OK",
            "strengthConfig": {
                "strength": c.base_strength,
                "randomStrength": c.random_strength,
            }
        })

    def _resp_current_pulse(self, cid: str):
        ids = self.mgr.resolve_client_ids(cid)
        if not ids:
            return _json_response(self, 200, {
                "status": 1, "code": "OK", "pulseId": "d6f83af0"
            })
        c = self.mgr.get_client(ids[0])
        if c.pulse_mode == "single":
            return _json_response(self, 200, {
                "status": 1, "code": "OK", "pulseId": c.current_pulse_id
            })
        return _json_response(self, 200, {
            "status": 1, "code": "OK", "pulseId": c.pulse_list_ids
        })

    def _resp_pulse_list(self, include_custom: bool):
        return _json_response(self, 200, {
            "status": 1, "code": "OK",
            "pulseList": self.mgr.pulse_list
        })

    def _resp_server_info(self):
        """返回服务器信息，包含 DG-Lab APP 扫码所需的 WebSocket 连接URL。

        与 DGLabGameHub 的 GET /api/server_info 完全兼容：
          {
            "status": 1, "code": "OK",
            "server": {
              "wsUrl": "/ws/",
              "clientWsUrls": [{"domain": "...", "connectUrl": "..."}],
              "apiBaseHttpUrl": "http://127.0.0.1:8920"
            }
          }
        """
        # 优先从 ws_manager 获取（与 WS 服务器实际监听端口一致）
        ws_manager = getattr(self.server, "ws_manager", None)
        port = getattr(self.server, "port", DEFAULT_PORT)
        if ws_manager is not None:
            client_ws_urls = ws_manager.get_connect_urls()
        else:
            client_ws_urls = build_connect_urls(port)

        api_base = f"http://127.0.0.1:{port}"
        return _json_response(self, 200, {
            "status": 1, "code": "OK",
            "server": {
                "wsUrl": "/ws/",
                "clientWsUrls": client_ws_urls,
                "apiBaseHttpUrl": api_base,
            },
        })

    def _resp_client_connect(self):
        """申请新 clientId（DG-Lab APP 连接前调用）。

        生成一个唯一的 clientId，APP 扫码后用此 clientId 连接 WebSocket。
        """
        # 生成 clientId 并确保不与已连接客户端冲突
        ws_manager = getattr(self.server, "ws_manager", None)
        new_cid = ""
        if ws_manager is not None:
            for _ in range(10):
                cid = str(uuid.uuid4())
                if not ws_manager.get_client(cid):
                    new_cid = cid
                    break
        else:
            new_cid = str(uuid.uuid4())

        if not new_cid:
            return _json_response(self, 500, {
                "status": 0, "code": "ERR::CREATE_CLIENT_ID_FAILED",
                "message": "无法创建唯一的客户端ID，请稍后重试",
            })
        return _json_response(self, 200, {
            "status": 1, "code": "OK", "clientId": new_cid,
        })

    def _post_set_strength(self, cid: str, data: dict):
        s = data.get("strength")
        rs = data.get("randomStrength")
        ok = self.mgr.broadcast_set_strength(
            cid, strength=s, random_strength=rs)
        return _json_response(self, 200, {
            "status": 1, "code": "OK",
            "message": f"成功设置了 {len(ok)} 个游戏的强度配置",
            "successClientIds": ok,
        })

    def _post_set_pulse(self, cid: str, data: dict):
        pid = data.get("pulseId")
        if pid is None and isinstance(data.get("pulseId[]"), list):
            pid = data["pulseId[]"]
        ok = self.mgr.broadcast_set_pulse(cid, pid)
        return _json_response(self, 200, {
            "status": 1, "code": "OK",
            "message": f"成功设置了 {len(ok)} 个游戏的波形ID",
            "successClientIds": ok,
        })

    def _post_fire(self, cid: str, data: dict):
        strength = int(data.get("strength", 20))
        time_ms = int(data.get("time", 5000))
        override = bool(data.get("override", False))
        pulse_id = data.get("pulseId")
        ok = self.mgr.broadcast_fire(
            cid, strength=strength, time_ms=time_ms,
            override=override, pulse_id=pulse_id)
        return _json_response(self, 200, {
            "status": 1, "code": "OK",
            "message": f"成功向 {len(ok)} 个游戏发送了一键开火指令",
            "successClientIds": ok,
        })

    def _compat_legacy(self, path: str, data: dict):
        """兼容旧版路径（调试面板曾用）。"""
        cid = "all"
        if path == "/api/strength" or path == "/strength":
            s = data.get("strength") or data
            return self._post_set_strength(cid, {"strength": s})
        if path == "/api/action":
            # 兼容一键开火的老格式
            st = data.get("strength", 20)
            t = data.get("time", data.get("time_ms", 5000))
            return self._post_fire(cid, {"strength": st, "time": t})
        return _json_response(self, 404, {"status": 0, "code": "ERR::NOT_FOUND"})

    def _err_client(self, cid: str):
        return {"status": 0, "code": "ERR::CLIENT_NOT_FOUND",
                "message": f"客户端 {cid} 未连接"}


class CoyoteHttpServer:
    """内置 Coyote Game Hub HTTP 服务器包装类。"""

    def __init__(self, manager: "coyote_device.CoyoteDeviceManager" = None,
                 host: str = "0.0.0.0", port: int = 8920):
        self.manager = manager or coyote_device.get_default_manager()
        self.host = host
        self.port = port
        self.ws_manager = None  # 关联的 WebSocket 管理器（共用端口模式时设置）
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def set_ws_manager(self, ws_manager):
        """关联 WebSocket 管理器（用于 /api/server_info 等端点）。"""
        self.ws_manager = ws_manager
        # 同步到已运行的 HTTP server 实例
        if self._server is not None:
            self._server.ws_manager = ws_manager

    def start(self) -> bool:
        """启动 HTTP 服务器（独立占用端口）。"""
        if self._running:
            return True
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), CoyoteApiHandler)
            self._server.manager = self.manager
            self._server.ws_manager = self.ws_manager
            self._server.port = self.port
            self._thread = threading.Thread(
                target=self._serve, daemon=True, name="CoyoteHttpServer")
            self._thread.start()
            self._running = True
            logger.info(f"Coyote HTTP Server 启动: http://{self.host}:{self.port}")
            return True
        except OSError as e:
            logger.warning(f"Coyote HTTP Server 启动失败（端口占用？）: {e}")
            self._server = None
            return False

    def stop(self):
        """停止 HTTP 服务器。"""
        self._running = False
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self._server = None
        self._thread = None

    def _serve(self):
        try:
            self._server.serve_forever(poll_interval=0.5)
        except Exception as e:
            logger.warning(f"Coyote HTTP Server 异常退出: {e}")
        finally:
            self._running = False


# ── 独立请求处理函数（供 ws_server 复用端口时调用）──

def handle_http_request(method: str, path: str, headers, body: bytes,
                        manager=None, ws_manager=None,
                        port: int = DEFAULT_PORT):
    """处理一个 HTTP 请求，返回 (status, headers_list, body_bytes) 或 None。

    供 coyote_ws_server 的 process_request 钩子调用，让 HTTP 和 WebSocket
    共用同一个端口（8920）。当请求路径不是 WebSocket 升级时，由此函数处理。

    注意：websockets 的 process_request 无法读取 POST body，所以只支持 GET。
    """
    parsed = urlparse(path)
    path_clean = parsed.path.rstrip("/")

    def _ok(data: dict, status: int = 200):
        b = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return (status, [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(b))),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ], b)

    # 健康检查
    if path_clean in ("", "/", "/health", "/status"):
        clients_count = len(manager.list_clients()) if manager else 0
        return _ok({
            "status": 1, "code": "OK",
            "message": "内置 Coyote Game Hub Server 运行中（WS+HTTP 共用端口）",
            "clients": clients_count,
        })

    # 服务器信息（含 WebSocket 连接URL）
    if path_clean == "/api/server_info":
        if ws_manager is not None:
            client_ws_urls = ws_manager.get_connect_urls()
        else:
            client_ws_urls = build_connect_urls(port)
        return _ok({
            "status": 1, "code": "OK",
            "server": {
                "wsUrl": "/ws/",
                "clientWsUrls": client_ws_urls,
                "apiBaseHttpUrl": f"http://127.0.0.1:{port}",
            },
        })

    # 申请新 clientId
    if path_clean == "/api/client/connect":
        new_cid = ""
        if ws_manager is not None:
            for _ in range(10):
                cid = str(uuid.uuid4())
                if not ws_manager.get_client(cid):
                    new_cid = cid
                    break
        else:
            new_cid = str(uuid.uuid4())
        if not new_cid:
            return _ok({"status": 0, "code": "ERR::CREATE_CLIENT_ID_FAILED",
                        "message": "无法创建唯一的客户端ID"}, 500)
        return _ok({"status": 1, "code": "OK", "clientId": new_cid})

    # 波形列表
    if path_clean == "/api/v2/pulse_list":
        if manager is None:
            return _ok({"status": 0, "code": "ERR::NO_MANAGER"}, 500)
        return _ok({"status": 1, "code": "OK", "pulseList": manager.pulse_list})

    # 404
    return _ok({"status": 0, "code": "ERR::NOT_FOUND",
                "message": f"404 {path}"}, 404)


# 全局单例
_default_server = None


def get_default_server() -> CoyoteHttpServer:
    global _default_server
    if _default_server is None:
        _default_server = CoyoteHttpServer()
    return _default_server
