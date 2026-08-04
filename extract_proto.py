"""
从 Proto DLL 中提取 FileDescriptorProto - 第四版
策略：搜索 protobuf 编码的消息名模式 (0x22 <len> 0x0a <len> <name>)
"""
from google.protobuf import descriptor_pb2

def find_protobuf_message_patterns(data):
    """搜索 protobuf 编码的消息名模式"""
    # 在 FileDescriptorProto 中，message_type 是 field 4 (tag 0x22)
    # 在 DescriptorProto 中，name 是 field 1 (tag 0x0a)
    # 所以模式是: 0x22 <varint_len> 0x0a <varint_len> <message_name>

    known_messages = [
        'SyncNearDeltaInfo', 'SyncToMeDeltaInfo', 'SyncNearEntities',
        'AoiSyncToMeDelta', 'CharBaseInfo', 'CurHp', 'MaxHp',
        'PlayerSyncPos', 'AttrHp', 'AttrMaxHp',
        'HpLessenValue', 'IsShowHp', 'HasHp',
        'BodyPartMaxHp', 'BossHpPercent',
    ]

    found_positions = []

    for msg_name in known_messages:
        name_bytes = msg_name.encode('ascii')
        name_len = len(name_bytes)

        # 构建搜索模式: 0x0a <name_len> <name>
        pattern = bytes([0x0a, name_len]) + name_bytes

        start = 0
        while True:
            idx = data.find(pattern, start)
            if idx == -1:
                break

            # 检查前面是否有 0x22 <descriptor_len>
            if idx >= 2:
                desc_tag = data[idx - 2]
                desc_len = data[idx - 1]
                if desc_tag == 0x22 and desc_len >= name_len + 2:
                    found_positions.append((idx - 2, msg_name))
                    # print(f"  [找到] {msg_name} at 0x{idx-2:08x}")

            start = idx + 1

    return found_positions

def try_parse_fd_from_position(data, pos, max_back=200000):
    """从消息位置向前搜索 FileDescriptorProto 起始位置"""
    # 向前搜索 0x0a + varint + ".proto"
    for back in range(1, max_back):
        p = pos - back
        if p < 0:
            break
        if data[p] != 0x0a:
            continue

        # 读取 varint 长度
        j = p + 1
        length = 0
        shift = 0
        while j < len(data) and j < p + 6:
            b = data[j]
            length |= (b & 0x7f) << shift
            j += 1
            shift += 7
            if (b & 0x80) == 0:
                break
        else:
            continue

        if length < 5 or length > 200:
            continue

        name_start = j
        if name_start + length > len(data):
            continue

        name_bytes = data[name_start:name_start + length]
        if not name_bytes.endswith(b'.proto'):
            continue
        if not all(32 <= b <= 126 for b in name_bytes):
            continue

        # 尝试解析
        try:
            fd = descriptor_pb2.FileDescriptorProto()
            fd.ParseFromString(data[p:])
            if len(fd.message_type) > 0 or len(fd.enum_type) > 0:
                return p, fd
        except Exception:
            continue

    return None, None

def print_message(msg, indent=0):
    """递归打印消息定义"""
    sp = "  " * indent
    print(f"{sp}message {msg.name} {{")

    for field in msg.field:
        label = ""
        if field.label == descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL:
            label = "optional "
        elif field.label == descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED:
            label = "repeated "

        type_name = field.type_name if field.type_name else field.Type.Name(field.type)
        if type_name.startswith("."):
            type_name = type_name[1:]

        print(f"{sp}  {label}{type_name} {field.name} = {field.number};")

    for nested in msg.nested_type:
        print_message(nested, indent + 1)

    for enum in msg.enum_type:
        print(f"{sp}  enum {enum.name} {{")
        for val in enum.value:
            print(f"{sp}    {val.name} = {val.number};")
        print(f"{sp}  }}")

    print(f"{sp}}}")

# 主程序
dll_path = r'H:\下载\StarResonanceDpsAnalysis-193\StarResonanceDpsAnalysis.Proto.dll'
print(f"正在分析: {dll_path}")

with open(dll_path, 'rb') as f:
    data = f.read()
print(f"文件大小: {len(data)} bytes")

# 1. 搜索 protobuf 编码的消息名模式
print("\n=== 搜索 protobuf 编码的消息名模式 ===")
found = find_protobuf_message_patterns(data)
print(f"找到 {len(found)} 个匹配位置")
for pos, name in found:
    print(f"  0x{pos:08x}: {name}")

# 2. 从每个位置向前搜索 FileDescriptorProto
print("\n=== 搜索 FileDescriptorProto ===")
unique_protos = {}
for pos, msg_name in found:
    fd_pos, fd = try_parse_fd_from_position(data, pos)
    if fd is not None:
        if fd.name not in unique_protos:
            unique_protos[fd.name] = (fd_pos, fd)
            print(f"  [成功] {fd.name} (偏移 0x{fd_pos:08x}, messages: {len(fd.message_type)}, enums: {len(fd.enum_type)})")

print(f"\n成功解析 {len(unique_protos)} 个 .proto 文件")

if unique_protos:
    # 打印所有文件名
    print("\n=== 所有 .proto 文件 ===")
    for name, (pos, fd) in sorted(unique_protos.items()):
        print(f"  {name} (package: {fd.package}, messages: {len(fd.message_type)}, enums: {len(fd.enum_type)})")

    # 搜索包含 HP 相关字段的消息
    print("\n\n=== 包含 HP 字段的消息 ===")
    for name, (pos, fd) in sorted(unique_protos.items()):
        for msg in fd.message_type:
            has_hp = any('hp' in f.name.lower() or 'health' in f.name.lower() for f in msg.field)
            if has_hp:
                print(f"\n// File: {name}")
                print_message(msg)

    # 搜索 Sync/Delta 相关消息
    print("\n\n=== Sync/Delta 相关消息 ===")
    sync_keywords = ['sync', 'delta', 'near', 'tome', 'aoi']
    for name, (pos, fd) in sorted(unique_protos.items()):
        for msg in fd.message_type:
            msg_name_lower = msg.name.lower()
            if any(kw in msg_name_lower for kw in sync_keywords):
                print(f"\n// File: {name}")
                print_message(msg)

    # 保存完整输出
    output_path = r'C:\Users\17110\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\work-mode-projects\6a6392a2f961ac5b3aecaff8\extracted_protos.txt'
    with open(output_path, 'w', encoding='utf-8') as f:
        import sys
        old_stdout = sys.stdout
        sys.stdout = f
        for name, (pos, fd) in sorted(unique_protos.items()):
            print(f"\n{'='*60}")
            print(f"// File: {name}")
            print(f"// Package: {fd.package}")
            if fd.syntax:
                print(f"// Syntax: {fd.syntax}")
            for dep in fd.dependency:
                print(f'import "{dep}";')
            print()
            for msg in fd.message_type:
                print_message(msg)
                print()
            for enum in fd.enum_type:
                print(f"enum {enum.name} {{")
                for val in enum.value:
                    print(f"  {val.name} = {val.number};")
                print(f"}}")
                print()
        sys.stdout = old_stdout
    print(f"\n完整定义已保存到: {output_path}")
