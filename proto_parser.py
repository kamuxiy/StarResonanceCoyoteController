"""
通用 Protobuf 解析器 - 不依赖 .proto 定义，直接解析 wire format。

参考: https://protobuf.dev/programming-guides/encoding/

Wire types:
  0 = Varint (int32, int64, uint32, uint64, sint32, sint64, bool, enum)
  1 = 64-bit (fixed64, sfixed64, double)
  2 = Length-delimited (string, bytes, embedded messages, packed repeated fields)
  5 = 32-bit (fixed32, sfixed32, float)
"""
import struct
import zstandard
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field


@dataclass
class ProtoField:
    """单个 protobuf 字段"""
    field_number: int
    wire_type: int
    value: Any  # int/float/str/bytes/list[ProtoField]


@dataclass
class ParsedMessage:
    """解析后的 protobuf 消息"""
    fields: List[ProtoField] = field(default_factory=list)

    def get(self, field_number: int) -> List[Any]:
        """获取指定字段编号的所有值"""
        return [f.value for f in self.fields if f.field_number == field_number]

    def get_first(self, field_number: int, default=None) -> Any:
        """获取指定字段编号的第一个值"""
        vals = self.get(field_number)
        return vals[0] if vals else default

    def find_int_values(self, min_val: int = 1000, max_val: int = 10_000_000) -> List[Tuple[int, int]]:
        """查找所有在指定范围内的整数字段值，返回 [(field_number, value), ...]
        用于查找 HP/MaxHp 这类数值字段（参考日志 hp=326536）"""
        results = []
        for f in self.fields:
            if f.wire_type == 0 and isinstance(f.value, int):
                if min_val <= f.value <= max_val:
                    results.append((f.field_number, f.value))
            elif f.wire_type == 2 and isinstance(f.value, list):
                # 嵌套消息递归查找
                nested = ParsedMessage(f.value)
                for fn, v in nested.find_int_values(min_val, max_val):
                    results.append((f.field_number, v))  # 只记外层字段号
        return results

    def to_dict(self, max_depth: int = 5) -> Dict:
        """转换为字典（用于日志输出）"""
        if max_depth <= 0:
            return {"_truncated": True}
        result = {}
        for f in self.fields:
            key = f"f{f.field_number}"
            if f.wire_type == 2 and isinstance(f.value, list):
                # 尝试作为嵌套消息解析
                try:
                    nested = ParsedMessage(f.value)
                    val = nested.to_dict(max_depth - 1)
                except Exception:
                    val = f.value[:64].hex() if isinstance(f.value, bytes) else str(f.value)
            elif f.wire_type == 2 and isinstance(f.value, bytes):
                val = f.value[:64].hex()
            else:
                val = f.value
            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(val)
            else:
                result[key] = val
        return result

    def summary(self, indent: int = 0) -> str:
        """生成可读的摘要"""
        lines = []
        prefix = "  " * indent
        for f in self.fields:
            if f.wire_type == 0:
                lines.append(f"{prefix}f{f.field_number} (varint): {f.value}")
            elif f.wire_type == 1:
                lines.append(f"{prefix}f{f.field_number} (64bit): {f.value}")
            elif f.wire_type == 2:
                if isinstance(f.value, list):
                    # 尝试作为嵌套消息
                    try:
                        nested = ParsedMessage(f.value)
                        nested_str = nested.summary(indent + 1)
                        lines.append(f"{prefix}f{f.field_number} (msg):")
                        lines.append(nested_str)
                    except Exception:
                        hex_str = bytes(f.value)[:32].hex()
                        lines.append(f"{prefix}f{f.field_number} (bytes[{len(f.value)}]): {hex_str}...")
                else:
                    hex_str = bytes(f.value)[:32].hex()
                    lines.append(f"{prefix}f{f.field_number} (bytes[{len(f.value)}]): {hex_str}")
            elif f.wire_type == 5:
                lines.append(f"{prefix}f{f.field_number} (32bit): {f.value}")
        return "\n".join(lines)


def read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    """读取 varint，返回 (value, new_offset)"""
    result = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            return result, offset
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")
    raise ValueError("varint truncated")


def parse_protobuf(data: bytes) -> ParsedMessage:
    """解析 protobuf 字节流（不依赖 .proto 定义）"""
    msg = ParsedMessage()
    offset = 0

    while offset < len(data):
        try:
            tag, offset = read_varint(data, offset)
        except ValueError:
            break

        field_number = tag >> 3
        wire_type = tag & 0x07

        if field_number == 0:
            break

        if wire_type == 0:  # Varint
            value, offset = read_varint(data, offset)
            # zigzag 解码尝试（如果是 sint32/sint64）
            # 但这里保留原始值，因为不确定是否 zigzag
            msg.fields.append(ProtoField(field_number, wire_type, value))

        elif wire_type == 1:  # 64-bit
            if offset + 8 > len(data):
                break
            value = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
            msg.fields.append(ProtoField(field_number, wire_type, value))

        elif wire_type == 2:  # Length-delimited
            length, offset = read_varint(data, offset)
            if offset + length > len(data):
                # 长度超出，可能解析错误，截断
                length = len(data) - offset
            value = data[offset:offset + length]
            offset += length

            # 尝试判断是字符串、嵌套消息还是字节数组
            # 先尝试作为嵌套消息解析
            try:
                nested = parse_protobuf(value)
                # 如果成功解析且有合理字段号，作为嵌套消息
                if nested.fields and all(1 <= f.field_number < 10000 for f in nested.fields):
                    msg.fields.append(ProtoField(field_number, wire_type, nested.fields))
                else:
                    msg.fields.append(ProtoField(field_number, wire_type, value))
            except Exception:
                msg.fields.append(ProtoField(field_number, wire_type, value))

        elif wire_type == 5:  # 32-bit
            if offset + 4 > len(data):
                break
            value = struct.unpack("<I", data[offset:offset + 4])[0]
            offset += 4
            msg.fields.append(ProtoField(field_number, wire_type, value))

        elif wire_type == 3 or wire_type == 4:  # 已废弃的组
            # 跳过
            break
        else:
            break

    return msg


def try_zstd_decompress(data: bytes) -> Optional[bytes]:
    """尝试 zstd 解压"""
    try:
        dctx = zstandard.ZstdDecompressor()
        return dctx.decompress(data)
    except Exception:
        # 可能是带帧头的，或者不是zstd
        try:
            dctx = zstandard.ZstdDecompressor()
            # 尝试流式解压
            return dctx.decompress(data, max_output_size=10 * 1024 * 1024)
        except Exception:
            return None


def find_hp_candidates(msg: ParsedMessage, known_uid: Optional[int] = None) -> List[Dict]:
    """
    在解析后的消息中查找可能是 HP/MaxHp 的字段。
    参考: 日志中 hp=326536, maxHp=326536 (玩家), enemy attrId 10030=12232

    返回: [{"field": field_number, "value": value, "path": "..."}, ...]
    """
    candidates = []
    # HP 范围: 100 ~ 10,000,000 (覆盖玩家和小怪)
    # 优先查找成对出现的相同值 (hp == maxHp 的情况)
    int_fields = {}

    def walk(m: ParsedMessage, path: str = ""):
        for f in m.fields:
            cur_path = f"{path}.f{f.field_number}" if path else f"f{f.field_number}"
            if f.wire_type == 0 and isinstance(f.value, int):
                if 100 <= f.value <= 10_000_000:
                    int_fields.setdefault(f.field_number, []).append((f.value, cur_path))
            elif f.wire_type == 2 and isinstance(f.value, list):
                try:
                    nested = ParsedMessage(f.value)
                    walk(nested, cur_path)
                except Exception:
                    pass

    walk(msg)

    # 找出出现频率高且数值合理的字段
    for field_num, values in int_fields.items():
        for val, path in values:
            candidates.append({
                "field": field_num,
                "value": val,
                "path": path
            })

    # 按数值大小排序（HP通常较大）
    candidates.sort(key=lambda x: -x["value"])
    return candidates
