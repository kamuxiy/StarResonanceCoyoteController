"""DGLab / Coyote Game Hub 客户端。

双重模式（自动切换）：
 1. 内置模式：当内置 coyote_http_server 在运行时，直接调用 CoyoteDeviceManager 的方法，
    不走网络，零延迟。
 2. 外部模式：内置服务器未启动时，通过 HTTP REST 连接外部 DGLabGameController / CoyoteGameHub。

默认端口 8920，API 完全兼容 DG-Lab-Coyote-Game-Hub v2 规范。
"""
import json
import urllib.request
import urllib.error
import threading
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8920
DEFAULT_CLIENT_ID = "all"


# ── 模式辅助 ──
def _is_builtin_server_running() -> bool:
    try:
        import coyote_http_server
        srv = coyote_http_server.get_default_server()
        return srv.running
    except Exception:
        return False


def _builtin_manager():
    """获取内置设备管理器（仅当服务器在运行时有效）。"""
    try:
        import coyote_device
        return coyote_device.get_default_manager()
    except Exception:
        return None


class DGLabClient:
    """DGLabGameController / CoyoteGameHub 客户端（内置/外部 双模式）。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 client_id: str = DEFAULT_CLIENT_ID):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._lock = threading.Lock()

    # ── 模式判定 ──
    @property
    def mode(self) -> str:
        """返回 'builtin' 或 'external'。"""
        return "builtin" if _is_builtin_server_running() else "external"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── HTTP 辅助（外部模式）──
    def _post_http(self, path: str, body: dict, timeout: float = 3.0) -> dict:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.URLError as e:
            return {"status": 0, "code": "ERR::NETWORK", "message": str(e.reason)}
        except Exception as e:
            return {"status": 0, "code": "ERR::UNKNOWN", "message": str(e)}

    def _get_http(self, path: str, timeout: float = 3.0) -> dict:
        url = self.base_url + path
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.URLError as e:
            return {"status": 0, "code": "ERR::NETWORK", "message": str(e.reason)}
        except Exception as e:
            return {"status": 0, "code": "ERR::UNKNOWN", "message": str(e)}

    # ── 统一 API ──

    def fire(self, strength: int = 20, time_ms: int = 5000,
             override: bool = False, pulse_id: str = None) -> dict:
        """一键开火（触发惩罚）。强度 0-40，时间 ms（最高30000）。"""
        strength = min(int(strength), 40)
        time_ms = min(int(time_ms), 30000)

        if self.mode == "builtin":
            mgr = _builtin_manager()
            ok = mgr.broadcast_fire(
                self.client_id, strength=strength,
                time_ms=time_ms, override=override, pulse_id=pulse_id)
            return {
                "status": 1, "code": "OK",
                "message": f"成功向 {len(ok)} 个设备发送开火指令",
                "successClientIds": ok,
            }
        body = {"strength": strength, "time": time_ms, "override": override}
        if pulse_id:
            body["pulseId"] = pulse_id
        return self._post_http(
            f"/api/v2/game/{self.client_id}/action/fire", body)

    def set_strength(self, set: int = None, set_val: int = None,
                     add: int = None, sub: int = None) -> dict:
        """设置基础强度（strength config）。

        兼容两种参数名：`set` (符合 REST API 语义) 或 `set_val` (Python 友好)。
        """
        s_dict = {}
        if set is not None:
            s_dict["set"] = int(set)
        elif set_val is not None:
            s_dict["set"] = int(set_val)
        if add is not None:
            s_dict["add"] = int(add)
        if sub is not None:
            s_dict["sub"] = int(sub)
        if not s_dict:
            return {"status": 0, "code": "ERR::PARAM", "message": "需指定 set/add/sub"}

        if self.mode == "builtin":
            mgr = _builtin_manager()
            ok = mgr.broadcast_set_strength(self.client_id, strength=s_dict)
            return {
                "status": 1, "code": "OK",
                "message": f"成功设置 {len(ok)} 个设备强度",
                "successClientIds": ok,
            }
        return self._post_http(
            f"/api/v2/game/{self.client_id}/strength", {"strength": s_dict})

    def set_random_strength(self, set: int = None, set_val: int = None,
                            add: int = None, sub: int = None) -> dict:
        """设置随机强度。"""
        rs_dict = {}
        if set is not None:
            rs_dict["set"] = int(set)
        elif set_val is not None:
            rs_dict["set"] = int(set_val)
        if add is not None:
            rs_dict["add"] = int(add)
        if sub is not None:
            rs_dict["sub"] = int(sub)
        if not rs_dict:
            return {"status": 0, "code": "ERR::PARAM", "message": "需指定 set/add/sub"}

        if self.mode == "builtin":
            mgr = _builtin_manager()
            ok = mgr.broadcast_set_strength(self.client_id,
                                            random_strength=rs_dict)
            return {
                "status": 1, "code": "OK",
                "message": f"成功设置 {len(ok)} 个设备随机强度",
                "successClientIds": ok,
            }
        return self._post_http(
            f"/api/v2/game/{self.client_id}/strength",
            {"randomStrength": rs_dict})

    def set_pulse(self, pulse_id) -> dict:
        """设置当前波形。pulse_id 可以是字符串或列表。"""
        if self.mode == "builtin":
            mgr = _builtin_manager()
            ok = mgr.broadcast_set_pulse(self.client_id, pulse_id)
            return {
                "status": 1, "code": "OK",
                "message": f"成功设置 {len(ok)} 个设备波形",
                "successClientIds": ok,
            }
        return self._post_http(
            f"/api/v2/game/{self.client_id}/pulse", {"pulseId": pulse_id})

    def get_game_info(self) -> dict:
        """获取游戏信息（强度、波形等）。"""
        if self.mode == "builtin":
            mgr = _builtin_manager()
            ids = mgr.resolve_client_ids(self.client_id)
            if not ids:
                return {"status": 0, "code": "ERR::CLIENT_NOT_FOUND",
                        "message": f"客户端 {self.client_id} 未连接"}
            c = mgr.get_client(ids[0])
            return c.to_game_info()
        return self._get_http(f"/api/v2/game/{self.client_id}")

    def get_pulse_list(self) -> dict:
        """获取波形列表。"""
        if self.mode == "builtin":
            mgr = _builtin_manager()
            return {"status": 1, "code": "OK", "pulseList": mgr.pulse_list}
        return self._get_http("/api/v2/pulse_list")

    def is_online(self) -> bool:
        """检查后端（内置或外部）是否可用。"""
        if self.mode == "builtin":
            return True
        result = self.get_pulse_list()
        return result.get("status") == 1

    # ── 便捷方法：异步（不阻塞 UI）──

    def fire_async(self, strength: int = 20, time_ms: int = 5000,
                   override: bool = False, pulse_id: str = None):
        def _do():
            with self._lock:
                self.fire(strength, time_ms, override, pulse_id)
        threading.Thread(target=_do, daemon=True).start()

    def set_strength_async(self, **kwargs):
        def _do():
            with self._lock:
                self.set_strength(**kwargs)
        threading.Thread(target=_do, daemon=True).start()

    def set_random_strength_async(self, **kwargs):
        def _do():
            with self._lock:
                self.set_random_strength(**kwargs)
        threading.Thread(target=_do, daemon=True).start()


# 全局单例
_default_client = None


def get_default_client() -> DGLabClient:
    global _default_client
    if _default_client is None:
        _default_client = DGLabClient()
    return _default_client
