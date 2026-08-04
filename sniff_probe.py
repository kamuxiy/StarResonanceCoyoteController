"""
抓包探测脚本：验证能否抓到星痕共鸣游戏流量，分析消息分帧格式。

目标服务器: 58.217.183.115 (TCP, 动态端口)
参考实现: StarResonanceDpsAnalysis-193 (SharpPcap + PacketDotNet + Google.Protobuf)

用法: python sniff_probe.py [时长秒数] [网卡过滤关键词]
"""
import sys
import os
import time
import struct
from collections import defaultdict, Counter
from datetime import datetime

# scapy for packet capture
from scapy.all import sniff, IP, TCP, Raw, get_if_list, conf

# 目标服务器IP (来自参考项目日志)
TARGET_IP = "58.217.183.115"

def list_interfaces():
    """列出所有可用网卡"""
    ifs = get_if_list()
    print(f"[+] 可用网卡 ({len(ifs)} 个):")
    for i, ifname in enumerate(ifs):
        print(f"  [{i}] {ifname}")
    return ifs

def find_game_interface():
    """尝试找到能抓到游戏流量的网卡"""
    ifs = get_if_list()
    # Windows 上 Npcap 的网卡名通常是 \Device\NPF_{GUID} 或以太网描述
    print(f"[+] 共 {len(ifs)} 个网卡")
    for ifname in ifs:
        print(f"  - {ifname}")
    return ifs

def analyze_frame_format(data):
    """
    分析单条TCP数据的分帧格式。
    参考项目推断: [长度][opcode][protobuf payload]
    """
    if len(data) < 4:
        return None
    
    results = []
    
    # 尝试不同分帧格式:
    # 格式A: 4字节大端长度 + 2字节大端opcode + payload
    if len(data) >= 6:
        length_be = struct.unpack(">I", data[:4])[0]
        if 4 < length_be <= len(data) and length_be < 100000:
            opcode_be = struct.unpack(">H", data[4:6])[0]
            results.append(("A_BE_len4_op2", length_be, opcode_be, data[6:6+length_be-2] if length_be >= 2 else b""))
    
    # 格式B: 4字节小端长度 + 2字节小端opcode + payload
    if len(data) >= 6:
        length_le = struct.unpack("<I", data[:4])[0]
        if 4 < length_le <= len(data) and length_le < 100000:
            opcode_le = struct.unpack("<H", data[4:6])[0]
            results.append(("B_LE_len4_op2", length_le, opcode_le, data[6:6+length_le-2] if length_le >= 2 else b""))
    
    # 格式C: 2字节大端长度 + 2字节大端opcode + payload
    if len(data) >= 4:
        length_be2 = struct.unpack(">H", data[:2])[0]
        if 2 < length_be2 <= len(data) and length_be2 < 50000:
            opcode_be2 = struct.unpack(">H", data[2:4])[0]
            results.append(("C_BE_len2_op2", length_be2, opcode_be2, data[4:4+length_be2-2] if length_be2 >= 2 else b""))
    
    # 格式D: 2字节小端长度 + 2字节小端opcode + payload
    if len(data) >= 4:
        length_le2 = struct.unpack("<H", data[:2])[0]
        if 2 < length_le2 <= len(data) and length_le2 < 50000:
            opcode_le2 = struct.unpack("<H", data[2:4])[0]
            results.append(("D_LE_len2_op2", length_le2, opcode_le2, data[4:4+length_le2-2] if length_le2 >= 2 else b""))
    
    return results

def packet_handler(pkt):
    """数据包处理回调"""
    if not pkt.haslayer(IP) or not pkt.haslayer(TCP):
        return
    
    ip = pkt[IP]
    tcp = pkt[TCP]
    
    # 只关注目标IP的流量
    if ip.src != TARGET_IP and ip.dst != TARGET_IP:
        return
    
    if not pkt.haslayer(Raw):
        return
    
    payload = bytes(pkt[Raw].load)
    if len(payload) < 4:
        return
    
    direction = "S->C" if ip.src == TARGET_IP else "C->S"
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    
    print(f"\n[{ts}] {direction} {ip.sport}->{ip.dport} len={len(payload)}")
    print(f"  hex_head: {payload[:32].hex()}")
    
    # 分析分帧
    frames = analyze_frame_format(payload)
    if frames:
        for fmt_name, length, opcode, frame_payload in frames[:2]:
            print(f"  {fmt_name}: len={length} op={opcode}(0x{opcode:04x}) payload_len={len(frame_payload)}")


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    if_filter = sys.argv[2] if len(sys.argv) > 2 else None
    
    print("=" * 70)
    print("星痕共鸣 - 抓包探测脚本")
    print(f"目标服务器: {TARGET_IP} (TCP)")
    print(f"抓包时长: {duration} 秒")
    print("=" * 70)
    
    # 列出网卡
    list_interfaces()
    print()
    
    # BPF过滤器: 只抓目标IP的TCP流量
    bpf = f"tcp and host {TARGET_IP}"
    print(f"[+] BPF过滤器: {bpf}")
    print(f"[+] 开始抓包... (等待游戏流量)")
    print("[!] 请确保游戏正在运行且有网络活动")
    print()
    
    # 统计
    pkt_count = [0]
    byte_count = [0]
    flows = defaultdict(lambda: {"packets": 0, "bytes": 0})
    
    def stats_handler(pkt):
        if not pkt.haslayer(IP) or not pkt.haslayer(TCP):
            return
        ip = pkt[IP]
        tcp = pkt[TCP]
        if ip.src != TARGET_IP and ip.dst != TARGET_IP:
            return
        if not pkt.haslayer(Raw):
            return
        payload = bytes(pkt[Raw].load)
        key = (ip.sport, ip.dport, "S->C" if ip.src == TARGET_IP else "C->S")
        flows[key]["packets"] += 1
        flows[key]["bytes"] += len(payload)
        pkt_count[0] += 1
        byte_count[0] += len(payload)
        # 打印前20个包的详细内容
        if pkt_count[0] <= 20:
            packet_handler(pkt)
    
    try:
        sniff(filter=bpf, prn=stats_handler, timeout=duration, store=False)
    except PermissionError:
        print("[ERROR] 需要管理员权限运行抓包!")
        print("  请以管理员身份打开终端再运行此脚本")
        return
    except Exception as e:
        print(f"[ERROR] 抓包失败: {e}")
        import traceback
        traceback.print_exc()
        return
    
    print()
    print("=" * 70)
    print("抓包统计")
    print("=" * 70)
    print(f"总数据包: {pkt_count[0]}")
    print(f"总字节数: {byte_count[0]}")
    print(f"活跃流: {len(flows)}")
    print()
    
    if flows:
        print("流量分布:")
        for key, stats in sorted(flows.items(), key=lambda x: -x[1]["bytes"]):
            sport, dport, direction = key
            print(f"  {direction} {sport}->{dport}: {stats['packets']}包, {stats['bytes']}字节")
    
    if pkt_count[0] == 0:
        print()
        print("[!] 未抓到任何流量，可能原因:")
        print(f"  1. 游戏未连接到 {TARGET_IP}")
        print("  2. 网卡选择错误 (scapy默认抓所有网卡)")
        print("  3. 需要管理员权限")
        print("  4. 游戏使用VPN/代理，IP已变化")


if __name__ == "__main__":
    main()
