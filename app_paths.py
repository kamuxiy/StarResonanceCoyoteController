"""统一的应用路径解析器，兼容：
- 开发环境（直接 python main.py）
- PyInstaller onefile：exe 被解压到 sys._MEIPASS（临时目录）
- PyInstaller onedir：  所有资源放在 exe 同级目录下

优先顺序（每个资源都遵循）：
  1) 可执行文件 / main.py 所在目录的同级（用户可手动替换资源）
  2) sys._MEIPASS（PyInstaller 解压出来的只读区）
  3) 源码目录（os.path.dirname(__file__)，仅开发环境）
"""
from __future__ import annotations

import os
import sys
from typing import Optional


# ── 基础定位 ────────────────────────────────────────────────

def app_base_dir() -> str:
    """程序"外部工作根"——PyInstaller 时是 exe 同级；开发时是 main.py 同级。
    用来存放：debug_screenshots、用户写的 users.json、SRDC 抓包程序（可替换）、用户导入波形目录等。
    允许用户在不改打包内容的前提下直接替换/新增文件。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller: sys.executable = E:/星痕强度控制器/星痕强度控制器.exe
        return os.path.dirname(os.path.abspath(sys.executable))
    # 开发环境
    return os.path.dirname(os.path.abspath(sys.argv[0] if len(sys.argv) else __file__))


def app_bundle_dir() -> str:
    """程序"只读内置资源根"——PyInstaller 时是 sys._MEIPASS；开发时等于源码目录。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def dev_src_dir() -> str:
    """开发环境源码根（仅当 app_base/app_bundle 都找不到资源时回退到 E:\\CODE 原始位置）。"""
    return os.path.dirname(os.path.abspath(__file__))


# ── 通用资源查找 ────────────────────────────────────────────

def find_resource(relpath: str, must_exist: bool = False) -> Optional[str]:
    """按 [外部同级 → 打包内置 → 开发源码] 顺序找资源，返回绝对路径；找不到时：
    must_exist=False 时返回"外部目录 + relpath"（作为创建路径也合理）。
    must_exist=True 时返回 None。"""
    candidates = [
        os.path.join(app_base_dir(), relpath),
        os.path.join(app_bundle_dir(), relpath),
        os.path.join(dev_src_dir(), relpath),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    if must_exist:
        return None
    # 没找到但也不要求存在：返回"优先写入位置"（外部目录）
    return os.path.abspath(candidates[0])


def ensure_dir_for(abs_path: str) -> str:
    """确保 abs_path 父目录存在，返回 abs_path。"""
    parent = os.path.dirname(os.path.abspath(abs_path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    return abs_path


# ── 常用路径的语义化封装 ────────────────────────────────────

def debug_screenshots_dir() -> str:
    """OCR 调试截图输出目录（允许用户直接查看，放在 exe 同级）。"""
    p = os.path.join(app_base_dir(), "debug_screenshots")
    os.makedirs(p, exist_ok=True)
    return p


def users_json_path() -> str:
    """users.json：星痕共鸣历史/导入波形等用户态数据，写在 exe 同级。"""
    return ensure_dir_for(os.path.join(app_base_dir(), "users.json"))


# SRDC 抓包程序：允许 1) 用户把 StarResonanceDamageCounter-master 整个放到 exe 同级
#                 2) 打包时放进 one-dir 根目录
#                 3) 开发环境 E:\CODE\...
_SRDC_DIR_NAME = "StarResonanceDamageCounter-master"


def srdc_dir() -> str:
    return find_resource(_SRDC_DIR_NAME) or os.path.join(app_base_dir(), _SRDC_DIR_NAME)


def srdc_server_js() -> str:
    return os.path.join(srdc_dir(), "server.js")


def srdc_api_url() -> str:
    return "http://localhost:8989/api/data"


def pulse_importer_root() -> str:
    """pulse_loader 导入/保存波形的根目录——也用外部同级（用户能看到）。"""
    return app_base_dir()
