"""
深度协议分析脚本：
1. 验证分帧格式 (4B BE length + 2B BE opcode + 20B header + protobuf)
2. 解析protobuf消息结构
3. 统计opcode分布
4. 查找HP相关字段

已确认的格式 (来自抓包探测):
  [4B BE total_len]   - 消息总长度 (从opcode开始计算, 不含自身)
  [2B BE opcode]      - 消息类型
  [20B header]        - 额外头部 (flags, req_id, ack_id, channel, seq)
  [protobuf payload]  - protobuf 消息体

已知 opcode:
  1 - 请求 (C->S)
  2 - 响应 (S->C)
  3 - ACK/响应 (S->C, 无protobuf)
  4 - 心跳 (双向, 无payload)
"""
import sys
import os
import struct
import time
from collections import defaultdict
from datetime import datetime

from scapy.all import sniff, IP, TCP, Raw, get_if_list, conf

from proto_parser import parse_protobuf, ParsedMessage, try_zstd_decompress, find_hp_candidates

TARGET_IP = "58.217.183.115"
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def parse_message(data: bytes, direction: str) -> dict:
    """解析单条消息，返回解析结果字典"""
    result = {
        "raw": data,
        "direction": direction,
        "valid": False,
        "length": 0,
        "opcode": 0,
        "header": {},
        "protobuf": None,
        "error": None,
    }

    if len(data) < 6:
        result["error"] = "too_short"
        return result

    total_len = struct.unpack(">I", data[:4])[0]
    opcode = struct.unpack(">H", data[4:6])[0]

    result["length"] = total_len
    result["opcode"] = opcode

    # 检查长度是否合理
    if total_len < 2 or total_len > 10_000_000:
        result["error"] = f"bad_length:{total_len}"
        return result

    payload_len = total_len - 2  # 减去opcode
    if len(data) < 4 + total_len:
        result["error"] = f"incomplete: need {4+total_len}, have {len(data)}"
        return result

    payload = data[6:6 + payload_len]

    # 尝试解析20字节头部
    if payload_len >= 20:
        header_bytes = payload[:20]
        proto_bytes = payload[20:]

        flags = struct.unpack(">I", header_bytes[0:4])[0]
        req_id = struct.unpack(">I", header_bytes[4:8])[0]
        ack_id = struct.unpack(">I", header_bytes[8:12])[0]
        channel = struct.unpack(">I", header_bytes[12:16])[0]
        seq = struct.unpack(">I", header_bytes[16:20])[0]

        result["header"] = {
            "flags": flags,
            "req_id": req_id,
            "ack_id": ack_id,
            "channel": channel,
            "seq": seq,
        }

        # 尝试解析 protobuf
        if proto_bytes and len(proto_bytes) > 0:
            try:
                # 尝试zstd解压
                decompressed = try_zstd_decompress(proto_bytes)
                if decompressed:
                    proto_bytes = decompressed
                    result["zstd_decompressed"] = True

                msg = parse_protobuf(proto_bytes)
                if msg.fields:
                    result["protobuf"] = msg
                    result["valid"] = True
                else:
                    result["error"] = "empty_protobuf"
            except Exception as e:
                result["error"] = f"parse_error:{e}"
        else:
            result["valid"] = True  # 心跳等无payload的消息也是有效的
    else:
        # payload < 20 字节，没有完整头部
        result["header"]["_raw"] = payload.hex()
        result["valid"] = True if opcode == 4 else False

    return result


def print_message_detail(result: dict, idx: int):
    """打印消息详情"""
    op = result["opcode"]
    direction = result["direction"]
    length = result["length"]

    print(f"\n{'='*70}")
    print(f"消息 #{idx}  [{direction}] op=0x{op:04x}({op}) len={length}")
    print(f"{'='*70}")

    if result["header"]:
        h = result["header"]
        print(f"  Header:")
        print(f"    flags=0x{h.get('flags', 0):08x}")
        print(f"    req_id=0x{h.get('req_id', 0):08x} ({h.get('req_id', 0)})")
        print(f"    ack_id=0x{h.get('ack_id', 0):08x} ({h.get('ack_id', 0)})")
        print(f"    channel=0x{h.get('channel', 0):08x} ({h.get('channel', 0)})")
        print(f"    seq=0x{h.get('seq', 0):08x} ({h.get('seq', 0)})")

    if result.get("zstd_decompressed"):
        print(f"  [zstd 已解压]")

    if result["protobuf"]:
        msg = result["protobuf"]
        print(f"  Protobuf 字段数: {len(msg.fields)}")
        print(f"  结构摘要:")
        print(msg.summary(indent=2))

        # 查找HP候选值
        hp_candidates = find_hp_candidates(msg)
        if hp_candidates:
            print(f"\n  HP/数值 候选字段:")
            for c in hp_candidates[:10]:
                print(f"    f{c['field']} = {c['value']}  @ {c['path']}")
    else:
        print(f"  无 Protobuf 数据 ({result.get('error', 'empty')})")


def main():
    print("=" * 70)
    print("星痕共鸣 - 深度协议分析")
    print(f"目标: {TARGET_IP}  时长: {DURATION}s")
    print("=" * 70)

    # 统计
    opcode_counts = defaultdict(int)
    channel_counts = defaultdict(int)
    messages = []
    pkt_count = 0
    byte_count = 0
    reassembly_buf = defaultdict(bytes)  # 每个方向的流缓冲

    def on_packet(pkt):
        nonlocal pkt_count, byte_count
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
        pkt_count += 1
        byte_count += len(payload)

        # 添加到重组缓冲
        buf_key = direction
        reassembly_buf[buf_key] += payload

        # 尝试从缓冲中解析完整消息
        buf = reassembly_buf[buf_key]
        consumed = 0

        while len(buf) - consumed >= 6:
            total_len = struct.unpack(">I", buf[consumed:consumed + 4])[0]
            if total_len < 2 or total_len > 1_000_000:
                break
            if len(buf) - consumed < 4 + total_len:
                break  # 不完整，等下一个包

            msg_data = buf[consumed:consumed + 4 + total_len]
            result = parse_message(msg_data, direction)
            messages.append(result)

            opcode_counts[result["opcode"]] += 1
            if result["header"].get("channel"):
                channel_counts[result["header"]["channel"]] += 1

            consumed += 4 + total_len

        # 清理已消费的数据
        if consumed > 0:
            reassembly_buf[buf_key] = buf[consumed:]

    print(f"[+] 开始抓包 {DURATION} 秒...")
    print()

    try:
        sniff(filter=f"tcp and host {TARGET_IP}", prn=on_packet, timeout=DURATION, store=False)
    except Exception as e:
        print(f"[ERROR] 抓包失败: {e}")
        import traceback
        traceback.print_exc()
        return

    print()
    print("=" * 70)
    print("统计报告")
    print("=" * 70)
    print(f"TCP段数: {pkt_count}")
    print(f"总字节: {byte_count}")
    print(f"完整消息数: {len(messages)}")
    print()

    print(f"Opcode 分布:")
    for op, count in sorted(opcode_counts.items(), key=lambda x: -x[1]):
        print(f"  op=0x{op:04x}({op:>5d}): {count:>4d} 条")
    print()

    if channel_counts:
        print(f"Channel 分布:")
        for ch, count in sorted(channel_counts.items(), key=lambda x: -x[1]):
            print(f"  ch=0x{ch:08x}({ch:>10d}): {count:>4d} 条")
        print()

    # 打印前10条非心跳消息的详细解析
    non_heartbeat = [m for m in messages if m["opcode"] not in (4,)]
    print(f"\n详细分析前 {min(10, len(non_heartbeat))} 条非心跳消息:")
    for i, msg in enumerate(non_heartbeat[:10]):
        print_message_detail(msg, i + 1)

    # 统计大消息
    large_msgs = [m for m in messages if m["length"] > 10000]
    if large_msgs:
        print(f"\n\n{'='*70}")
        print(f"大消息统计 (>10KB): {len(large_msgs)} 条")
        print(f"{'='*70}")
        for msg in sorted(large_msgs, key=lambda x: -x["length"])[:5]:
            print(f"  [{msg['direction']}] op=0x{msg['opcode']:04x} len={msg['length']:,} bytes")
            if msg["protobuf"]:
                hp = find_hp_candidates(msg["protobuf"])
                if hp:
                    print(f"    HP候选: {[(c['field'], c['value']) for c in hp[:5]]}")

    # 汇总所有找到的HP候选值
    all_hp = []
    for msg in messages:
        if msg["protobuf"]:
            hps = find_hp_candidates(msg["protobuf"])
            for h in hps:
                all_hp.append((h["value"], h["path"], msg["direction"], msg["opcode"]))

    if all_hp:
        print(f"\n\n{'='*70}")
        print(f"全部数值候选 (范围100~10,000,000): {len(all_hp)} 个")
        print(f"{'='*70}")
        # 按数值分组统计出现频率
        from collections import Counter
        value_freq = Counter(v for v, _, _, _ in all_hp)
        print("Top 20 高频数值:")
        for val, freq in value_freq.most_common(20):
            paths = set(p for v, p, _, _ in all_hp if v == val)
            path_str = list(paths)[0] if paths else "?"
            print(f"  {val:>12,}  出现{freq:>3}次  路径: {path_str}")


if __name__ == "__main__":
    main()
