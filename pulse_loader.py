"""波形导入与解析模块。

支持三种波形文件格式：
  1. pulse.json5 格式（DGLabGameController 内置）—— 已在 coyote_device.BUILTIN_PULSES 中
  2. .pulse 单文件格式（Dungeonlab+pulse:...）—— DG-Lab 编辑器导出
  3. .pulse 集合文件（JSON 数组，含 points1/points2/points3）—— 波形集合

解析后将 points 数据转换为 DG-Lab 协议的 pulseData 十六进制字符串。
每个 pulseData 条目 = 16 个十六进制字符（8 字节）：
  前 4 字节 = 频率（4 个 2.5ms 时隙）
  后 4 字节 = 强度（0x00=0%, 0x64=100%）
每个条目代表 10ms 的波形数据。
"""
import json
import os
import uuid
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 默认频率（10Hz，与大多数内置波形一致）
DEFAULT_FREQ = 0x0A
# 每个点的 x 单位对应的毫秒数（插值精度）
MS_PER_POINT = 100


def _int_to_hex2(v: int) -> str:
    """将 0-255 的整数转换为 2 位大写十六进制字符串。"""
    return format(max(0, min(255, int(v))), "02X")


def _y_to_intensity(y_100: float) -> int:
    """将 0-100 的 y 值映射为 0-100 的强度值（0x00-0x64）。"""
    return max(0, min(100, round(y_100 * 100 / 100)))


def points_to_pulse_data(points: list, y_scale: float = 1.0,
                         freq: int = DEFAULT_FREQ) -> list:
    """将点列表转换为 pulseData 十六进制字符串列表。

    Args:
        points: [{"x": int, "y": float, "anchor": int}, ...]
        y_scale: y 值缩放因子（.pulse 格式 y=0-100 则 1.0，JSON 格式 y=0-20 则 5.0）
        freq: 频率值（0x0A=10Hz, 0x64=100Hz, 0xBE=190Hz）

    Returns: ["0A0A0A0A64646464", ...] 每个 10ms
    """
    if not points:
        return ["0A0A0A0A00000000"]

    # 按 x 排序
    pts = sorted(points, key=lambda p: p.get("x", 0))
    freq_hex = _int_to_hex2(freq)

    # 线性插值生成 10ms 粒度的强度值
    entries = []
    x_start = int(pts[0].get("x", 0))
    x_end = int(pts[-1].get("x", 0))
    if x_end <= x_start:
        x_end = x_start + 1

    # 每个 x 单位 = MS_PER_POINT ms，每 10ms 一个条目
    total_steps = max(1, (x_end - x_start) * MS_PER_POINT // 10)

    for step in range(total_steps):
        t = x_start + step * 10 / MS_PER_POINT
        # 找到 t 所在的区间
        y_val = _interpolate_y(pts, t, y_scale)
        intensity = _y_to_intensity(y_val)
        intensity_hex = _int_to_hex2(intensity)
        entries.append(f"{freq_hex * 4}{intensity_hex * 4}")

    # 确保至少有一个条目
    if not entries:
        entries.append(f"{freq_hex * 4}00000000")
    return entries


def _interpolate_y(points: list, t: float, y_scale: float) -> float:
    """线性插值计算时间 t 处的 y 值。"""
    pts = sorted(points, key=lambda p: p.get("x", 0))
    for i in range(len(pts) - 1):
        x0 = float(pts[i].get("x", 0))
        x1 = float(pts[i + 1].get("x", 0))
        if x0 <= t <= x1:
            y0 = float(pts[i].get("y", 0)) * y_scale
            y1 = float(pts[i + 1].get("y", 0)) * y_scale
            if x1 == x0:
                return y0
            ratio = (t - x0) / (x1 - x0)
            # anchor=1 表示线性，anchor=0 表示平滑（这里简化为线性）
            return y0 + (y1 - y0) * ratio
    # 超出范围，返回端点值
    if t <= float(pts[0].get("x", 0)):
        return float(pts[0].get("y", 0)) * y_scale
    return float(pts[-1].get("y", 0)) * y_scale


# ── .pulse 单文件格式解析（Dungeonlab+pulse:...）──

def parse_dungeonlab_pulse(content: str) -> Optional[dict]:
    """解析 Dungeonlab+pulse 格式的 .pulse 文件内容。

    格式示例：
      Dungeonlab+pulse:0,1,1=0,0,0,1,1/100.00-1,100.00-0,...+section+0,20,8,1,0/0.00-1,100.00-1

    Returns: {"name": ..., "pulseData": [...]} 或 None
    """
    content = content.strip()
    if not content.startswith("Dungeonlab+pulse:"):
        return None

    body = content[len("Dungeonlab+pulse:"):]
    # 分割头部和各 section
    parts = body.split("+section+")
    if not parts:
        return None

    # 第一部分: "0,1,1=0,0,0,1,1/100.00-1,100.00-0,..."
    first = parts[0]
    if "=" not in first:
        return None
    header, section1 = first.split("=", 1)
    # header = "0,1,1" (L, classic, defaultName)
    # section1 = "0,0,0,1,1/100.00-1,100.00-0,..."
    if "/" not in section1:
        return None
    params1, points1_str = section1.split("/", 1)
    points1 = _parse_pulse_points(points1_str, y_scale=1.0)

    # 后续 sections 是 points2, points3
    points2 = []
    points3 = []
    if len(parts) > 1 and "/" in parts[1]:
        _, pts2_str = parts[1].split("/", 1)
        points2 = _parse_pulse_points(pts2_str, y_scale=1.0)
    if len(parts) > 2 and "/" in parts[2]:
        _, pts3_str = parts[2].split("/", 1)
        points3 = _parse_pulse_points(pts3_str, y_scale=1.0)

    # 使用 points1（主波形）生成 pulseData
    pulse_data = points_to_pulse_data(points1, y_scale=1.0)
    # 合并 points2 如果有（叠加效果）
    if points2:
        pd2 = points_to_pulse_data(points2, y_scale=1.0)
        # 取较长的一个
        if len(pd2) > len(pulse_data):
            pulse_data = pd2

    return {
        "name": "导入波形",
        "pulseData": pulse_data,
        "_points1": points1,
        "_points2": points2,
        "_points3": points3,
    }


def _parse_pulse_points(pts_str: str, y_scale: float = 1.0) -> list:
    """解析 .pulse 文件中的点字符串。

    格式: "100.00-1,100.00-0,0.00-1" → [{y:100, anchor:1}, {y:100, anchor:0}, ...]
    """
    points = []
    for item in pts_str.split(","):
        item = item.strip()
        if "-" not in item:
            continue
        parts = item.split("-")
        if len(parts) < 2:
            continue
        try:
            y = float(parts[0])
            anchor = int(parts[1])
            points.append({"x": len(points), "y": y, "anchor": anchor})
        except (ValueError, IndexError):
            continue
    return points


# ── JSON 数组格式解析（波形集合.pulse）──

def parse_json_pulse_array(content: str) -> list:
    """解析 JSON 数组格式的波形集合文件。

    每个元素包含 waveName, points1, points2, points3 等字段。
    points 是 JSON 字符串，包含 [{anchor, x, y}, ...]，y 范围 0-20。

    Returns: [{"name": ..., "pulseData": [...]}, ...]
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 尝试逐行解析（每行一个 JSON 对象）
        results = []
        for line in content.strip().split("\n"):
            line = line.strip().rstrip(",")
            if not line or line == "[":
                continue
            try:
                obj = json.loads(line)
                pulse = _json_wave_to_pulse(obj)
                if pulse:
                    results.append(pulse)
            except json.JSONDecodeError:
                continue
        return results

    if isinstance(data, list):
        results = []
        for obj in data:
            pulse = _json_wave_to_pulse(obj)
            if pulse:
                results.append(pulse)
        return results
    return []


def _json_wave_to_pulse(obj: dict) -> Optional[dict]:
    """将 JSON 波形对象转换为标准 pulse 格式。"""
    if not isinstance(obj, dict):
        return None
    name = obj.get("waveName") or obj.get("name") or "未命名"
    # points1/2/3 是 JSON 字符串，y 范围 0-20，需缩放 5x 到 0-100
    points1 = _parse_json_points_str(obj.get("points1", ""))
    points2 = _parse_json_points_str(obj.get("points2", ""))
    points3 = _parse_json_points_str(obj.get("points3", ""))

    # 使用 points1 生成 pulseData（y 0-20 → 0-100，缩放 5x）
    pulse_data = points_to_pulse_data(points1, y_scale=5.0)
    if points2:
        pd2 = points_to_pulse_data(points2, y_scale=5.0)
        if len(pd2) > len(pulse_data):
            pulse_data = pd2

    return {
        "name": name,
        "pulseData": pulse_data,
        "_points1": points1,
        "_points2": points2,
        "_points3": points3,
    }


def _parse_json_points_str(pts_str: str) -> list:
    """解析 JSON 字符串格式的点列表。"""
    if not pts_str:
        return []
    try:
        pts = json.loads(pts_str)
        return [{"x": p.get("x", i), "y": p.get("y", 0), "anchor": p.get("anchor", 1)}
                for i, p in enumerate(pts)]
    except (json.JSONDecodeError, TypeError):
        return []


# ── 文件导入入口 ──

def import_pulse_file(filepath: str) -> list:
    """导入单个波形文件，返回解析出的波形列表。

    支持：
      - .pulse 单文件（Dungeonlab+pulse 格式）
      - .pulse 集合文件（JSON 数组格式）
      - .json 文件（JSON 数组格式）

    Returns: [{"id": ..., "name": ..., "pulseData": [...]}, ...]
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"读取波形文件失败 {filepath}: {e}")
        return []

    results = []
    # 尝试 Dungeonlab+pulse 格式
    if content.strip().startswith("Dungeonlab+pulse:"):
        pulse = parse_dungeonlab_pulse(content)
        if pulse:
            pulse["id"] = str(uuid.uuid4())[:8]
            pulse["name"] = os.path.splitext(os.path.basename(filepath))[0]
            results.append(pulse)
    else:
        # 尝试 JSON 数组格式
        pulses = parse_json_pulse_array(content)
        for p in pulses:
            p["id"] = str(uuid.uuid4())[:8]
            # 清理内部字段
            p.pop("_points1", None)
            p.pop("_points2", None)
            p.pop("_points3", None)
            results.append(p)

    return results


def import_pulse_directory(dirpath: str) -> list:
    """导入目录下所有 .pulse 文件。"""
    results = []
    try:
        for fname in os.listdir(dirpath):
            if fname.endswith(".pulse") or fname.endswith(".json"):
                fpath = os.path.join(dirpath, fname)
                results.extend(import_pulse_file(fpath))
    except Exception as e:
        logger.warning(f"扫描波形目录失败 {dirpath}: {e}")
    return results


# ── 波形预览数据生成 ──

def get_pulse_preview(pulse_data: list, max_points: int = 80) -> list:
    """从 pulseData 生成预览数据点（强度曲线）。

    Args:
        pulse_data: ["0A0A0A0A64646464", ...]
        max_points: 最多返回的点数（下采样）

    Returns: [{"time": i, "intensity": 0-100}, ...]
    """
    if not pulse_data:
        return []

    points = []
    step = max(1, len(pulse_data) // max_points)
    for i in range(0, len(pulse_data), step):
        entry = pulse_data[i]
        if len(entry) >= 16:
            # 后 4 字节是强度，取第 5 个字节（index 8-9）
            intensity_hex = entry[8:10]
            try:
                intensity = int(intensity_hex, 16)
                points.append({"time": i, "intensity": intensity})
            except ValueError:
                points.append({"time": i, "intensity": 0})
        else:
            points.append({"time": i, "intensity": 0})
    return points
