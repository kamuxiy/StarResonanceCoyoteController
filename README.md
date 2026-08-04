<div align="center">

# 星痕强度控制器 | StarResonanceCoyoteController

[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![PyQt5](https://img.shields.io/badge/GUI-PyQt5%20Fluent-green?style=flat-square)](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)
[![License](https://img.shields.io/badge/License-AGPL--3.0-orange?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078d7?style=flat-square&logo=windows&logoColor=white)](#)

---

一款面向 **星痕共鸣** 游戏的 DG-Lab 郊狼(Coyote)设备强度控制器

基于网络抓包解析血量数据 + OCR 截图识别双方案，自动触发强度控制与波形播放，通过 WebSocket 连接手机 APP 驱动实机 ✿✿ヽ(°▽°)ノ✿

</div>

---

## ✨ 亮点功能

| 功能 | 说明 |
| :---: | :--- |
| 🎯 **双方案采集** | 网络抓包（推荐）+ OCR 截图识别，自动读取玩家血量 |
| 📡 **网卡自动探测** | 启动抓包时自动检测所有网卡，优先物理网卡，自动切换到有效数据源 |
| ⚡ **死亡惩罚递增** | 玩家死亡后强度自动累加（可自定义增量），实时显示下次触发强度 |
| 🔁 **空闲波形循环** | 未触发时可循环播放内置/导入波形，可单独设置空闲强度 |
| 🔥 **一键开火事件** | 自定义事件（死亡/攻击等）触发高强度短脉冲 |
| 📱 **WebSocket 实机** | 扫码连接 DG-Lab APP，双通道强度控制 + 18 种内置波形 |
| 📊 **悬浮窗实时显示** | 桌面悬浮窗显示血量、强度、SRDA 状态、网卡信息 |
| 🧩 **PyInstaller 打包** | 单目录绿色版，免安装 Python 直接运行 |

---

## 🚀 快速开始

### 方式一：下载 Release（推荐新手）

1. 前往 [Releases](https://github.com/kamuxiy/StarResonanceCoyoteController/releases) 下载最新版 `星痕强度控制器.zip`
2. 解压到任意目录，双击 `星痕强度控制器.exe` 启动
3. 先启动 **星痕共鸣** 游戏，再选择抓包模式 / OCR 模式启动采集
4. 打开 DG-Lab APP 扫码连接郊狼设备，点击启用输出

> ⚠️ **抓包模式**需要**管理员权限**（右键 → 以管理员身份运行）

### 方式二：源码运行

```bash
# 1. 克隆仓库
git clone https://github.com/kamuxiy/StarResonanceCoyoteController.git
cd StarResonanceCoyoteController

# 2. 安装依赖
pip install -r requirements.txt

# 3. 安装 Node.js 抓包依赖
cd StarResonanceDamageCounter-master
npm install    # 或 pnpm install
cd ..

# 4. 启动程序
python main.py
```

---

## ⚙️ 环境依赖

### 运行环境

| 项目 | 要求 | 版本 |
| :--- | :--- | :--- |
| **操作系统** | Windows 10 / 11（64 位） | ✅ 支持 |
| **Python** | 源码运行需要 | 3.10 ~ 3.12 |
| **Node.js** | 抓包模式需要（已随 Release 打包） | 18+ |
| **管理员权限** | 网络抓包必需 | 抓包模式建议开启 |
| **Tesseract OCR** | OCR 模式可选 | 5.0+ |

### 核心依赖

| 库名称 | 作用 |
| :--- | :--- |
| [PyQt5](https://pypi.org/project/PyQt5/) | 图形界面框架 |
| [PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) | 现代化 Fluent Design 风格组件库 |
| [scapy](https://scapy.net/) | 网络抓包（物理网卡数据链路层） |
| [zstandard](https://pypi.org/project/zstandard/) | Protobuf payload 解压 |
| [Pillow](https://python-pillow.org/) | 截图处理与波形可视化 |
| [pytesseract](https://pypi.org/project/pytesseract/) | OCR 文字识别 |
| [websockets](https://pypi.org/project/websockets/) | WebSocket 服务器，连接 DG-Lab APP |
| [requests](https://pypi.org/project/requests/) | HTTP 请求（调用 SRDC 抓包 API） |
| [psutil](https://pypi.org/project/psutil/) | 进程监控与网卡枚举 |
| [pywin32](https://pypi.org/project/pywin32/) | Windows 窗口句柄操作、热键注册 |

---

## 🛠️ 创作工具

| 类别 | 工具 |
| :--- | :--- |
| 编程语言 | **Python 3.10** + **JavaScript (Node.js)**（抓包子项目） |
| GUI 框架 | PyQt5 + PyQt-Fluent-Widgets |
| 打包 | PyInstaller --onedir |
| IDE | VS Code / PyCharm |
| 网络调试 | Wireshark, Scapy, Chrome DevTools |
| 设计 | Figma（UI 参考）, qfluentwidgets Demos |

---

## 📁 项目结构与主要文件

```
StarResonanceCoyoteController/
├── main.py                      # 程序入口：全局异常处理 + QApplication 启动
├── config_window.py             # 主配置窗口：控制面板 / 血量状态 / 郊狼控制 / 关于
├── app_paths.py                 # ⭐ 资源路径管理（exe同级 → _MEIPASS → 源码目录 三级定位）
├── packet_capture.py            # 📡 网络抓包模块：SRDC Node.js 自动启动 + API 轮询
├── ocr_engine.py                # 📷 OCR 引擎：Tesseract 识别血量、名称、队伍
├── window_monitor.py            # 🖼️ 窗口监控：查找游戏句柄 / 区域截图
├── coyote_ws_server.py          # 🔌 WebSocket 服务器：DG-Lab APP 扫码通信协议
├── coyote_device.py             # 🎛️ 设备抽象层：双通道强度 + 18 种内置波形 + 开火事件
├── coyote_http_server.py        # HTTP 备用接口
├── pulse_loader.py              # 📀 自定义波形（.pulse/.pvf）导入
├── shared_state.py              # 💾 共享内存：跨进程强度状态
├── overlay_window.py            # 🪟 悬浮窗：血量条 + 强度 + SRDA 状态 + 网卡信息
├── proto_parser.py              # 🔒 Protobuf 协议解析 + HP 字段候选搜索
├── game_value_monitor.py        # 进程内存读取器
├── dglab_client.py              # DG-Lab 客户端辅助
├── requirements.txt             # Python 依赖列表
├── .gitignore                   # Git 忽略规则
└── StarResonanceDamageCounter-master/   # 📦 Node.js 抓包子项目（SRDC）
    ├── server.js                # Express + Socket.IO 抓包服务端
    ├── algo/blueprotobuf.js     # 游戏协议解析核心算法
    └── public/                  # SRDC 自带前端页面
```

---

## 📖 使用说明

### 1. 数据采集模式

| 模式 | 优点 | 缺点 |
| :--- | :--- | :--- |
| **网络抓包** ⭐推荐 | 数据 100% 准确，无延迟，不占截图资源 | 需要管理员权限，需 Node.js 环境 |
| **OCR 截图** | 兼容性好，不依赖协议 | 精度受分辨率/配色影响，有 1~2s 延迟 |

### 2. 郊狼设备连接

1. 手机打开 **DG-Lab APP** → **Socket 连接**
2. 对准程序二维码扫描 → 确认连接
3. APP 内点击 **连接郊狼**，开启输出
4. 程序内点击 **启用输出开关**，调整强度

### 3. 强度参数建议

| 参数 | 默认值 | 建议范围 | 说明 |
| :--- | :--- | :--- | :--- |
| 初始强度 | 15 | 5 ~ 25 | 启动时基础强度 |
| 强度上限 | 50 | 30 ~ 100 | 最大不超过此值（保护用） |
| 死亡增量 | 5 | 2 ~ 10 | 每次死亡加多少 |
| 持续时间 | 10s | 3 ~ 30s | 开火脉冲持续时长 |
| 空闲开关 | 关 | 开/关 | 是否循环播放基础波形 |
| 空闲强度 | 10 | 2 ~ 20 | 空闲时的波形输出强度 |

---

## 🤝 参考与致谢

### 参考项目

| 项目 | 作者 | 用途 |
| :--- | :--- | :--- |
| [StarResonanceDpsAnalysis](https://github.com/DannyDog/StarResonanceDps) | DannyDog | 🌟 抓包协议解析基础 / SRDC Node.js 算法参考 |
| [StarResonanceDps](https://github.com/anying1073/StarResonanceDps) | anying1073 | Windows C# 版协议实现与 RealtimeCapture 参考 |
| **DGLabGameController** | DG-Lab 社区 | 🎛️ 郊狼 Socket 协议 + 波形库（18 种内置波形）+ WebSocket 消息格式 |

### 开源协议

本项目使用 **[GNU Affero General Public License v3.0](LICENSE)** 开源：

- ✅ 允许个人使用、修改、分发
- ✅ 允许商业使用（AGPL 3.0 兼容）
- ⚠️ **修改后必须同样以 AGPL 3.0 开源**（即使仅通过网络提供服务）
- ⚠️ 分发时必须附带协议说明与版权声明
- ⚠️ 不得修改协议 / 闭源二次售卖

> 💡 DG-Lab 郊狼设备、波形数据版权归 **[地牢实验室 Dungeon Lab](https://www.dungeon-lab.com/)** 所有。
> 本项目仅供学习交流使用，请遵守当地法律法规与社区公约。

### 特别感谢

- 感谢 [DannyDog](https://github.com/DannyDog) 等先行者开源的 SRDC 抓包与协议逆向工作
- 感谢 [anying1073](https://github.com/anying1073) 的 C# 版实现与 RealtimeCapture 思路
- 感谢 DG-Lab 社区开发者提供的 Socket 协议文档与波形库
- 感谢 [PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) 作者提供优雅的 Fluent UI 组件

---

## 📮 反馈与参与

- 🐛 **Bug 反馈**：请在 [Issues](https://github.com/kamuxiy/StarResonanceCoyoteController/issues) 中描述复现步骤
- 💡 **功能建议**：欢迎提 Issue 或发起 PR
- 📜 **更新日志**：请查看 [Releases](https://github.com/kamuxiy/StarResonanceCoyoteController/releases) 页面

---

<div align="center">

**Made with ❤️ by kamuXiY**

*星痕共鸣 · 郊狼强度控制器 · Powered by Python + Node.js*

</div>
