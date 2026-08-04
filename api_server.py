"""
内置 HTTP API 服务 - 供 DGLabGameController / GameValueDetector 拉取游戏状态数据

本应用在某个端口（默认 6301）启动 HTTP 服务，暴露以下接口：
  GET  /api/health                       健康检查
  GET  /api/state                        完整状态（脉冲、加成、玩家列表、自身血量）
  GET  /api/players                      玩家列表（含自己）
  GET  /api/self                         自身血量+脉冲数据（GameValueDetector 最常用）
  GET  /api/pulse                        脉冲相关数值（current_pulse / next_bonus / one_click_bonus / trigger_count）

接口使用标准 JSON 返回格式。GameValueDetector 配置里可以通过监控这些接口返回的
JSON 字段值，实现「血量下降触发惩罚」「任意玩家阵亡触发开火」等联动效果。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, List


# 默认端口（与 DGLab / GameValueDetector 常用端口区分开，避免冲突）
DEFAULT_API_PORT = 6301


_shared_state_lock = threading.Lock()
_shared_state: Dict[str, Any] = {
    "app_name": "星痕共鸣·脉冲监控",
    "started_at": datetime.now().isoformat(timespec="seconds"),
    "current_pulse": 0,
    "next_bonus": 0,
    "one_click_bonus": 0,
    "trigger_count": 0,
    "bonus_condition": "任意玩家阵亡",
    "has_team_list": False,
    "self_health": {
        "name": "",
        "uid": 0,
        "profession": "",
        "current_hp": 0,
        "max_hp": 0,
        "health_percent": 0.0,
        "is_self": True,
        "is_dead": False,
    },
    # 玩家列表：每个元素包含一个玩家
    "players": [],
    # 额外标记：任意玩家阵亡
    "any_player_dead": False,
    "dead_players": [],
    # 服务器时间
    "timestamp": 0,
}


def update_shared_state(
    current_pulse: Optional[int] = None,
    next_bonus: Optional[int] = None,
    one_click_bonus: Optional[int] = None,
    trigger_count: Optional[int] = None,
    bonus_condition: Optional[str] = None,
    has_team_list: Optional[bool] = None,
    self_health: Optional[Any] = None,
    players: Optional[List[Any]] = None,
):
    """ConfigWindow / OCRWorker / 抓包线程 调用此函数，把最新状态写入共享内存。"""
    global _shared_state
    with _shared_state_lock:
        if current_pulse is not None:
            _shared_state["current_pulse"] = int(current_pulse)
        if next_bonus is not None:
            _shared_state["next_bonus"] = int(next_bonus)
        if one_click_bonus is not None:
            _shared_state["one_click_bonus"] = int(one_click_bonus)
        if trigger_count is not None:
            _shared_state["trigger_count"] = int(trigger_count)
        if bonus_condition is not None:
            _shared_state["bonus_condition"] = str(bonus_condition)
        if has_team_list is not None:
            _shared_state["has_team_list"] = bool(has_team_list)

        # 自身血量
        if self_health is not None:
            sh = _player_to_dict(self_health, is_self=True)
            _shared_state["self_health"] = sh

        # 玩家列表
        if players is not None:
            p_list: List[Dict[str, Any]] = []
            dead_list: List[Dict[str, Any]] = []
            for p in players:
                d = _player_to_dict(p, is_self=False)
                p_list.append(d)
                if d.get("is_dead"):
                    dead_list.append(d)
            _shared_state["players"] = p_list
            _shared_state["dead_players"] = dead_list
            _shared_state["any_player_dead"] = len(dead_list) > 0

        _shared_state["timestamp"] = time.time()


def _player_to_dict(p: Any, is_self: bool = False) -> Dict[str, Any]:
    """把 PlayerHealth / PlayerHealthInfo / dict 统一转换为 dict。"""
    if p is None:
        return {}
    if isinstance(p, dict):
        d = dict(p)
    else:
        d = {
            "name": getattr(p, "name", ""),
            "uid": int(getattr(p, "uid", 0)),
            "profession": getattr(p, "profession", ""),
            "current_hp": int(getattr(p, "current_hp", 0)),
            "max_hp": int(getattr(p, "max_hp", 0)),
            "health_percent": float(getattr(p, "health_percent", 0.0)),
            "level": int(getattr(p, "level", 0)),
            "is_self": bool(getattr(p, "is_self", is_self)),
        }
    # 补充 is_dead
    if "is_dead" not in d:
        hp = d.get("current_hp", 0) or 0
        pct = d.get("health_percent", 0) or 0
        d["is_dead"] = bool(hp <= 0 and pct <= 0)
    return d


def snapshot_state() -> Dict[str, Any]:
    """返回共享状态的快照（深拷贝）。"""
    with _shared_state_lock:
        snap = json.loads(json.dumps(_shared_state, ensure_ascii=False))
    return snap


# ===================== HTTP Handler =====================

class _StateApiHandler(BaseHTTPRequestHandler):
    server_version = "StarResonanceApi/1.0"

    def log_message(self, fmt, *args):
        # 静音：避免在 QThread 里 print 太多
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path in ("/api/health", "/healthz", "/health"):
            self._send_json({"code": 0, "message": "星痕共鸣 HTTP API 运行中", "timestamp": time.time()})
            return

        state = snapshot_state()

        if path == "/api/state":
            self._send_json({"code": 0, "data": state})
            return

        if path == "/api/players":
            self._send_json({
                "code": 0,
                "data": {
                    "self": state["self_health"],
                    "players": state["players"],
                    "any_player_dead": state["any_player_dead"],
                    "dead_players": state["dead_players"],
                    "has_team_list": state["has_team_list"],
                    "player_count": len(state["players"]),
                }
            })
            return

        if path in ("/api/self", "/api/self_health"):
            sh = state["self_health"]
            self._send_json({
                "code": 0,
                "data": {
                    **sh,
                    "current_pulse": state["current_pulse"],
                    "next_bonus": state["next_bonus"],
                    "one_click_bonus": state["one_click_bonus"],
                    "trigger_count": state["trigger_count"],
                    "bonus_condition": state["bonus_condition"],
                    "any_player_dead": state["any_player_dead"],
                    "dead_count": len(state["dead_players"]),
                }
            })
            return

        if path in ("/api/pulse", "/api/pulse_data"):
            self._send_json({
                "code": 0,
                "data": {
                    "current_pulse": state["current_pulse"],
                    "next_bonus": state["next_bonus"],
                    "one_click_bonus": state["one_click_bonus"],
                    "trigger_count": state["trigger_count"],
                    "bonus_condition": state["bonus_condition"],
                    "any_player_dead": state["any_player_dead"],
                    "dead_count": len(state["dead_players"]),
                }
            })
            return

        if path == "/api/dead":
            self._send_json({
                "code": 0,
                "data": {
                    "any_player_dead": state["any_player_dead"],
                    "dead_count": len(state["dead_players"]),
                    "dead_players": state["dead_players"],
                    "timestamp": state["timestamp"],
                }
            })
            return

        if path in ("/", "/api", ""):
            self._send_json({
                "code": 0,
                "message": "星痕共鸣脉冲监控 - HTTP API",
                "endpoints": {
                    "/api/health": "健康检查",
                    "/api/state": "完整状态快照",
                    "/api/players": "玩家列表+阵亡状态",
                    "/api/self": "自身血量+脉冲（常用）",
                    "/api/pulse": "脉冲数值+阵亡数（常用）",
                    "/api/dead": "仅阵亡状态（轻量）",
                },
                "howto": "把 GameValueDetector 配置文件中的 Monitor 指向以上 JSON 字段即可联动本应用",
            })
            return

        self._send_json({"code": 404, "message": "未知接口: " + path}, status=404)


class StateApiServer:
    """简单的 HTTP 服务器包装类，可在后台线程启动。"""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_API_PORT):
        self.host = host
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_running(self) -> bool:
        return self._server is not None

    def start(self) -> bool:
        if self._server is not None:
            return False
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), _StateApiHandler)
            self._server.daemon_threads = True
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            print(f"[HTTP API] 已启动 {self.base_url} (用于 DGLabGameController 联动)", flush=True)
            return True
        except OSError as e:
            print(f"[HTTP API] 端口 {self.port} 已被占用: {e}", flush=True)
            self._server = None
            return False

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None
            print("[HTTP API] 已停止", flush=True)


# 单例：全局默认 API 服务器
_default_server: Optional[StateApiServer] = None


def get_default_server(host: str = "127.0.0.1", port: int = DEFAULT_API_PORT) -> StateApiServer:
    global _default_server
    if _default_server is None:
        _default_server = StateApiServer(host=host, port=port)
    return _default_server


if __name__ == "__main__":
    # 单文件测试：直接启动并打印地址
    srv = StateApiServer()
    srv.start()
    print(f"测试访问: {srv.base_url}/api/self")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
