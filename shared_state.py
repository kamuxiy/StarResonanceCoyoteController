"""共享内存写入器（4级指针链版本）— 让 DGLabGameController 的 GameValueDetector 读取本应用数据。

参考 [In Falsus Demo][v0.2.2].json 的格式：
  Module + BaseAddress → 读指针P1 → P1 + Offsets[0] → 读指针P2
        → P2 + Offsets[1] → 读指针P3 → P3 + Offsets[2] → 读指针P4
        → P4 + Offsets[3] → 读值

所以构造 4 级指针链：
  S = Module基址 + BaseAddress = 共享内存起始地址
  [S + 0x000]  = P1 = S + 0x1000   (第1级指针表：写死1个指针)
  [P1 + A]     = P2 = S + 0x2000   (第2级指针表，A = Offsets[0])
  [P2 + B]     = P3 = S + 0x3000   (第3级指针表，B = Offsets[1])
  [P3 + C]     = P4 = S + 0x4000   (第4级指针表，C = Offsets[2])
  [P4 + D]     = 字段值            (D = Offsets[3])

固定所有 A=B=C=0x100，每个监控项用不同的 D（最后一级偏移）：
  Offsets = ["100", "100", "100", "<D>"]
  D 映射：
    0x00 → any_player_dead   (Int32)
    0x04 → dead_count        (Int32)
    0x08 → player_count      (Int32)
    0x0C → current_pulse     (Int32)
    0x10 → next_bonus        (Int32)
    0x14 → one_click_bonus   (Int32)
    0x18 → trigger_count     (Int32)
    0x1C → self_hp_percent   (Float)
    0x20 → self_current_hp   (Int32)
    0x24 → self_max_hp       (Int32)
"""
import ctypes
import os

# ── 常量 ──
SHARED_NAME = "StarResonanceGameState"
# 共享内存大小：4级指针 + 数据区（0x4100 足够）
SHARED_SIZE = 0x5000  # 20KB

# ── 指针链位置（全部相对于共享内存基址 S）──
PTR1_TBL = 0x0000   # P1 表：只有 1 项，存 S+0x1000
PTR2_TBL = 0x1000   # P2 表：A=0x100 处存 S+0x2000
PTR3_TBL = 0x2000   # P3 表：B=0x100 处存 S+0x3000
PTR4_TBL = 0x3000   # P4 表：C=0x100 处存 S+0x4000
DATA_AREA = 0x4000  # 实际数据区：P4+D -> 字段值

# ── 字段 D 偏移（最后一级偏移）──
DICT_OFFSETS = {
    "any_player_dead":   0x00,  # Int32
    "dead_count":        0x04,  # Int32
    "player_count":      0x08,  # Int32
    "current_pulse":     0x0C,  # Int32
    "next_bonus":        0x10,  # Int32
    "one_click_bonus":   0x14,  # Int32
    "trigger_count":     0x18,  # Int32
    "self_hp_percent":   0x1C,  # Float
    "self_current_hp":   0x20,  # Int32
    "self_max_hp":       0x24,  # Int32
}
# 字段类型
FIELD_TYPES = {
    "any_player_dead":   "Int32",
    "dead_count":        "Int32",
    "player_count":      "Int32",
    "current_pulse":     "Int32",
    "next_bonus":        "Int32",
    "one_click_bonus":   "Int32",
    "trigger_count":     "Int32",
    "self_hp_percent":   "Float",
    "self_current_hp":   "Int32",
    "self_max_hp":       "Int32",
}

# Windows API 常量
PAGE_READWRITE = 0x04
FILE_MAP_ALL_ACCESS = 0x000F001F


class SharedStateWriter:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._kernel32 = ctypes.windll.kernel32
        k32 = self._kernel32
        k32.CreateFileMappingW.restype = ctypes.c_void_p
        k32.CreateFileMappingW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p
        ]
        k32.MapViewOfFileEx.restype = ctypes.c_void_p
        k32.MapViewOfFileEx.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_size_t, ctypes.c_void_p
        ]
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_size_t
        ]
        k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self._mapping = None
        self._view = None
        self._base = 0
        self._started = False

    @property
    def base_address(self) -> int:
        return self._base

    @property
    def is_started(self) -> bool:
        return self._started

    # ── 指针读/写辅助 ──
    def _write_p(self, rel_off: int, value: int):
        """在 S+rel_off 处写入 64 位指针。"""
        p = ctypes.cast(ctypes.c_void_p(self._base + rel_off),
                        ctypes.POINTER(ctypes.c_uint64))
        p[0] = int(value)

    def _write_i32(self, rel_off: int, value: int):
        p = ctypes.cast(ctypes.c_void_p(self._base + rel_off),
                        ctypes.POINTER(ctypes.c_int32))
        p[0] = int(value)

    def _write_f(self, rel_off: int, value: float):
        p = ctypes.cast(ctypes.c_void_p(self._base + rel_off),
                        ctypes.POINTER(ctypes.c_float))
        p[0] = float(value)

    def start(self) -> bool:
        if self._started:
            return True
        k32 = self._kernel32

        # 创建命名文件映射（INVALID_HANDLE_VALUE = -1 → 页文件支持）
        mapping = k32.CreateFileMappingW(
            ctypes.c_void_p(-1), None,
            PAGE_READWRITE, 0, SHARED_SIZE,
            SHARED_NAME
        )
        if not mapping:
            return False
        self._mapping = mapping

        # 分配在 Module基址 + 大偏移处（保证 BaseAddress 为正）
        mod_base = self.get_module_base()
        preferred = []
        if mod_base > 0:
            for d in [0x08000000, 0x10000000, 0x20000000, 0x40000000]:
                preferred.append(mod_base + d)
        else:
            preferred = [0x10000000, 0x20000000, 0x40000000]

        base = 0
        view = None
        for addr in preferred:
            v = k32.MapViewOfFileEx(
                mapping, FILE_MAP_ALL_ACCESS,
                0, 0, SHARED_SIZE,
                ctypes.c_void_p(addr)
            )
            if v:
                view = v
                base = addr
                break
        if not view:
            v = k32.MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, SHARED_SIZE)
            if not v:
                k32.CloseHandle(mapping)
                self._mapping = None
                return False
            view = v
            base = ctypes.cast(view, ctypes.c_void_p).value
        self._view = view
        self._base = base
        self._started = True

        # ── 填充 4 级指针链（全部写好，保证 GameValueDetector 读到有效指针）──
        S = self._base
        self._write_p(PTR1_TBL + 0x000, S + PTR2_TBL)   # P1[0x000] = S+0x1000
        self._write_p(PTR2_TBL + 0x100, S + PTR3_TBL)   # P2[0x100] = S+0x2000
        self._write_p(PTR3_TBL + 0x100, S + PTR4_TBL)   # P3[0x100] = S+0x3000
        self._write_p(PTR4_TBL + 0x100, S + DATA_AREA)  # P4[0x100] = S+0x4000（数据区）

        # ── 初始化所有数据字段为 0 ──
        for fk, d in DICT_OFFSETS.items():
            if FIELD_TYPES[fk] == "Float":
                self._write_f(DATA_AREA + d, 0.0)
            else:
                self._write_i32(DATA_AREA + d, 0)
        return True

    def stop(self):
        if not self._started:
            return
        k32 = self._kernel32
        if self._view:
            k32.UnmapViewOfFile(ctypes.c_void_p(self._view))
        if self._mapping:
            k32.CloseHandle(ctypes.c_void_p(self._mapping))
        self._view = None
        self._mapping = None
        self._base = 0
        self._started = False

    # ── 字段更新 ──
    def update(self, current_pulse=None, next_bonus=None, one_click_bonus=None,
               trigger_count=None, self_hp_percent=None, self_current_hp=None,
               self_max_hp=None, any_player_dead=None, dead_count=None,
               player_count=None):
        if not self._started:
            return
        if any_player_dead is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["any_player_dead"], 1 if any_player_dead else 0)
        if dead_count is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["dead_count"], dead_count)
        if player_count is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["player_count"], player_count)
        if current_pulse is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["current_pulse"], current_pulse)
        if next_bonus is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["next_bonus"], next_bonus)
        if one_click_bonus is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["one_click_bonus"], one_click_bonus)
        if trigger_count is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["trigger_count"], trigger_count)
        if self_hp_percent is not None:
            self._write_f(DATA_AREA + DICT_OFFSETS["self_hp_percent"], self_hp_percent)
        if self_current_hp is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["self_current_hp"], self_current_hp)
        if self_max_hp is not None:
            self._write_i32(DATA_AREA + DICT_OFFSETS["self_max_hp"], self_max_hp)

    # ── JSON 配置生成辅助 ──
    def get_module_base(self, module_name: str = None) -> int:
        if module_name is None:
            module_name = os.path.basename(os.path.sys.executable)
        k32 = self._kernel32
        handle = k32.GetModuleHandleW(module_name)
        if handle:
            return int(handle)
        name_no_ext = os.path.splitext(module_name)[0]
        handle = k32.GetModuleHandleW(name_no_ext)
        return int(handle) if handle else 0

    def get_process_name(self) -> str:
        """返回不带 .exe 后缀的进程名（GameValueDetector 用 Process.ProcessName 匹配，不含扩展名）。"""
        full = os.path.basename(os.path.sys.executable)
        return os.path.splitext(full)[0]

    def get_module_name(self) -> str:
        """返回带 .exe 后缀的模块名（ProcessModule.ModuleName 含扩展名）。"""
        return os.path.basename(os.path.sys.executable)

    def get_base_address_for_json(self, module_name: str = None) -> str:
        """返回 BaseAddress（共享内存基址 - Module 基址）的 8 位十六进制字符串，无 0x 前缀。"""
        mod_base = self.get_module_base(module_name)
        if mod_base == 0:
            return "0"
        off = self._base - mod_base
        if off < 0:
            off = off & 0xFFFFFFFFFFFFFFFF
        # 对齐参考文件：8 位大写十六进制
        return f"{off:08X}"

    def get_offsets_for_field(self, field_key: str) -> list:
        """返回 4 个 Offsets（和参考文件一致的 4 级结构）。"""
        D = DICT_OFFSETS[field_key]
        return [
            "100",                 # P1[0x000] → P2，P2 + 0x100
            "100",                 # P3 + 0x100
            "100",                 # P4 + 0x100
            f"{D:X}",              # P4 + D → 实际值
        ]


# 对外导出字段映射
FIELD_OFFSETS = DICT_OFFSETS
FIELD_TYPE_MAP = FIELD_TYPES
