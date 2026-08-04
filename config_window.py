import sys
import os
import traceback
from datetime import datetime

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox, QFormLayout, QSizePolicy, QGridLayout, QFileDialog, QLineEdit
from PyQt5.QtGui import QIntValidator
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QColor, QPixmap, QPainter, QPen, QBrush
from PyQt5.QtCore import QByteArray, QIODevice
from PyQt5.QtGui import QImage

from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, isDarkTheme,
    PushButton, PrimaryPushButton, LineEdit, ComboBox, SpinBox, DoubleSpinBox,
    CardWidget, SubtitleLabel, CaptionLabel, BodyLabel,
    SwitchButton, TextEdit, Theme, setTheme,
    RadioButton, ScrollArea
)
from qfluentwidgets import FluentIcon as FIF

from window_monitor import WindowMonitor
from ocr_engine import OCREngine
from overlay_window import OverlayWindow
from packet_capture import PacketCaptureWorker, list_interfaces, is_admin, TARGET_IP
import app_paths


def debug_log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[DEBUG {timestamp}] [ConfigWindow] {message}", flush=True)


# OCR 调试截图输出目录：开发时在 E:\CODE\debug_screenshots；打包时在 exe 同级
DEBUG_DIR = app_paths.debug_screenshots_dir()
os.makedirs(DEBUG_DIR, exist_ok=True)


class OCRWorker(QThread):
    state_updated = pyqtSignal(object)
    error_message = pyqtSignal(str)
    window_found = pyqtSignal(bool)

    def __init__(self, window_keyword="星痕"):
        super().__init__()
        self.running = False
        self.window_monitor = WindowMonitor(window_keyword)
        self.ocr_engine = OCREngine()
        self.interval = 1.0
        self._debug_saved = False
        self._frame_count = 0

    def _save_debug_screenshots(self, full_img, health_img, name_img, team_img):
        try:
            timestamp = datetime.now().strftime("%H%M%S")
            if full_img:
                full_img.save(os.path.join(DEBUG_DIR, f"{timestamp}_full.png"))
                debug_log(f"已保存全屏截图: {timestamp}_full.png ({full_img.size[0]}x{full_img.size[1]})")
            if health_img:
                health_img.save(os.path.join(DEBUG_DIR, f"{timestamp}_health.png"))
                debug_log(f"已保存血量截图: {timestamp}_health.png ({health_img.size[0]}x{health_img.size[1]})")
            if name_img:
                name_img.save(os.path.join(DEBUG_DIR, f"{timestamp}_name.png"))
                debug_log(f"已保存名称截图: {timestamp}_name.png ({name_img.size[0]}x{name_img.size[1]})")
            if team_img:
                team_img.save(os.path.join(DEBUG_DIR, f"{timestamp}_team.png"))
                debug_log(f"已保存队伍截图: {timestamp}_team.png ({team_img.size[0]}x{team_img.size[1]})")
        except Exception as e:
            debug_log(f"保存调试截图失败: {e}")

    def run(self):
        debug_log("OCRWorker 线程启动!")
        self.running = True
        window_was_found = False

        while self.running:
            if not self.window_monitor.is_window_valid():
                debug_log("窗口无效，开始查找...")
                result = self.window_monitor.find_window()
                if result is None:
                    if window_was_found:
                        self.window_found.emit(False)
                        window_was_found = False
                    self.error_message.emit("未找到已启动的星痕共鸣客户端")
                    self.msleep(int(self.interval * 2000))
                    continue
                else:
                    if not window_was_found:
                        self.window_found.emit(True)
                        window_was_found = True
                        debug_log(f"窗口已找到: {result[1]}")

            debug_log("开始截图...")
            image = self.window_monitor.capture_window()
            if image:
                debug_log(f"截图成功: {image.size[0]}x{image.size[1]}")
                self._frame_count += 1
                state, changes = self.ocr_engine.process_image(image)

                health_img = None
                name_img = None
                team_img = None

                try:
                    health_img = self.window_monitor.capture_self_health()
                    if health_img:
                        debug_log(f"血量区域截图: {health_img.size[0]}x{health_img.size[1]}")
                        self_health = self.ocr_engine.parse_self_health_from_region(health_img)
                        debug_log(f"[血量检测] 图像分析: {self_health.health_percent:.1f}%, 数值OCR: {self_health.current_hp}/{self_health.max_hp}")
                        state.self_health = self_health
                        state.self_health.is_self = True
                    else:
                        debug_log("血量区域截图失败")
                except Exception as e:
                    debug_log(f"识别自身血量异常: {e}")
                    traceback.print_exc()

                try:
                    name_img = self.window_monitor.capture_player_name()
                    if name_img:
                        player_name = self.ocr_engine.parse_player_name_from_region(name_img)
                        if player_name:
                            debug_log(f"[名称检测] 玩家名: {player_name}")
                            state.self_health.name = player_name
                except Exception as e:
                    debug_log(f"识别玩家名称异常: {e}")

                try:
                    team_img = self.window_monitor.capture_team_list()
                    if team_img:
                        players, has_team = self.ocr_engine.parse_team_from_region(team_img)
                        if has_team:
                            debug_log(f"[队伍检测] 检测到队伍，{len(players)} 名成员")
                            for p in players:
                                debug_log(f"  - {p.name}: {p.health_percent:.1f}%")
                            state.has_team_list = True
                            state.players = players
                        else:
                            debug_log("[队伍检测] 未检测到队伍列表")
                except Exception as e:
                    debug_log(f"识别队伍列表异常: {e}")

                if not self._debug_saved and self._frame_count <= 3:
                    self._save_debug_screenshots(image, health_img, name_img, team_img)
                    if self._frame_count >= 3:
                        self._debug_saved = True
                        debug_log(f"调试截图已保存到: {DEBUG_DIR}")

                self.state_updated.emit(state)
            else:
                debug_log("截图失败!")

            self.msleep(int(self.interval * 1000))

    def stop(self):
        self.running = False
        self.wait()


class ControlInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ControlInterface")
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        title = SubtitleLabel("控制中心")
        title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Bold))
        layout.addWidget(title)

        # === 模式切换卡片 ===
        mode_card = CardWidget(self)
        mode_layout = QVBoxLayout(mode_card)
        mode_layout.setContentsMargins(20, 18, 20, 18)
        mode_layout.setSpacing(10)

        mode_title = BodyLabel("数据采集模式")
        mode_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        mode_layout.addWidget(mode_title)

        mode_desc = CaptionLabel("选择数据采集方式: OCR截图识别(旧) / 网络抓包解析(新, 更准确)")
        mode_desc.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        mode_layout.addWidget(mode_desc)

        mode_row = QHBoxLayout()
        self.radio_capture = RadioButton("抓包模式 (推荐)")
        self.radio_capture.setChecked(True)
        self.radio_ocr = RadioButton("OCR 截图模式")
        mode_row.addWidget(self.radio_capture)
        mode_row.addWidget(self.radio_ocr)
        mode_row.addStretch()
        mode_layout.addLayout(mode_row)

        layout.addWidget(mode_card)

        # === 抓包卡片 ===
        self.capture_card = CardWidget(self)
        cap_layout = QVBoxLayout(self.capture_card)
        cap_layout.setContentsMargins(20, 18, 20, 18)
        cap_layout.setSpacing(12)

        cap_header = QHBoxLayout()
        cap_title = BodyLabel("网络抓包监控")
        cap_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        cap_header.addWidget(cap_title)
        cap_header.addStretch()
        cap_layout.addLayout(cap_header)

        cap_desc = CaptionLabel(f"抓取游戏网络流量解析血量数据 (目标: {TARGET_IP})")
        cap_desc.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        cap_layout.addWidget(cap_desc)

        # 管理员权限状态
        admin_row = QHBoxLayout()
        admin_row.addWidget(BodyLabel("管理员权限"))
        admin_row.addStretch()
        self.admin_status_label = CaptionLabel("检测中...")
        if is_admin():
            self.admin_status_label.setText("已获取")
            self.admin_status_label.setTextColor(QColor(46, 204, 113), QColor(46, 204, 113))
        else:
            self.admin_status_label.setText("未获取 (需管理员运行)")
            self.admin_status_label.setTextColor(QColor(231, 76, 60), QColor(231, 76, 60))
        admin_row.addWidget(self.admin_status_label)
        cap_layout.addLayout(admin_row)

        # 网卡选择
        if_row = QHBoxLayout()
        if_row.addWidget(BodyLabel("网卡"))
        if_row.addStretch()
        self.combo_interface = ComboBox()
        self.combo_interface.setFixedWidth(300)
        try:
            ifs = list_interfaces()
            for guid, desc in ifs:
                self.combo_interface.addItem(desc, guid)
        except Exception as e:
            debug_log(f"列出网卡失败: {e}")
        if_row.addWidget(self.combo_interface)
        cap_layout.addLayout(if_row)

        # 目标IP显示
        target_row = QHBoxLayout()
        target_row.addWidget(BodyLabel("目标服务器"))
        target_row.addStretch()
        self.input_target_ip = LineEdit()
        self.input_target_ip.setText(TARGET_IP)
        self.input_target_ip.setFixedWidth(200)
        target_row.addWidget(self.input_target_ip)
        cap_layout.addLayout(target_row)

        # 启动按钮
        cap_btn_row = QHBoxLayout()
        self.btn_refresh_if = PushButton("刷新网卡")
        self.btn_refresh_if.setFixedHeight(36)
        self.btn_refresh_if.setIcon(FIF.SYNC)
        cap_btn_row.addWidget(self.btn_refresh_if)
        self.btn_start_capture = PrimaryPushButton("启动抓包")
        self.btn_start_capture.setFixedHeight(36)
        self.btn_start_capture.setIcon(FIF.PLAY)
        cap_btn_row.addWidget(self.btn_start_capture)
        cap_layout.addLayout(cap_btn_row)

        layout.addWidget(self.capture_card)

        # === OCR 卡片 ===
        self.ocr_card = CardWidget(self)
        ocr_layout = QVBoxLayout(self.ocr_card)
        ocr_layout.setContentsMargins(20, 18, 20, 18)
        ocr_layout.setSpacing(12)

        ocr_header = QHBoxLayout()
        ocr_title = BodyLabel("OCR 截图监控")
        ocr_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        ocr_header.addWidget(ocr_title)
        ocr_header.addStretch()
        ocr_layout.addLayout(ocr_header)

        ocr_desc = CaptionLabel("自动识别游戏窗口并读取游戏事件数据")
        ocr_desc.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        ocr_layout.addWidget(ocr_desc)

        keyword_layout = QHBoxLayout()
        keyword_layout.addWidget(BodyLabel("窗口关键词"))
        keyword_layout.addStretch()
        self.input_window_keyword = LineEdit()
        self.input_window_keyword.setText("Star")
        self.input_window_keyword.setFixedWidth(200)
        self.input_window_keyword.setPlaceholderText("窗口标题或进程名关键词")
        keyword_layout.addWidget(self.input_window_keyword)
        ocr_layout.addLayout(keyword_layout)

        btn_layout = QHBoxLayout()
        self.btn_scan_window = PushButton("扫描窗口")
        self.btn_scan_window.setFixedHeight(36)
        self.btn_scan_window.setIcon(FIF.SEARCH)
        btn_layout.addWidget(self.btn_scan_window)
        self.btn_start_ocr = PrimaryPushButton("启动截图器")
        self.btn_start_ocr.setFixedHeight(36)
        self.btn_start_ocr.setIcon(FIF.PLAY)
        btn_layout.addWidget(self.btn_start_ocr)
        ocr_layout.addLayout(btn_layout)

        overlay_layout = QHBoxLayout()
        overlay_label = BodyLabel("显示悬浮窗")
        overlay_layout.addWidget(overlay_label)
        overlay_layout.addStretch()
        self.switch_overlay = SwitchButton()
        self.switch_overlay.setChecked(True)
        overlay_layout.addWidget(self.switch_overlay)
        ocr_layout.addLayout(overlay_layout)

        layout.addWidget(self.ocr_card)

        log_card = CardWidget(self)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(20, 18, 20, 18)
        log_layout.setSpacing(10)

        log_title = BodyLabel("运行日志")
        log_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        log_layout.addWidget(log_title)

        self.log_text = TextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFixedHeight(160)
        log_layout.addWidget(self.log_text)

        layout.addWidget(log_card)
        layout.addStretch()

    def log(self, message: str):
        self.log_text.append(message)
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def set_capture_mode(self, enabled: bool):
        """切换抓包模式显示"""
        self.capture_card.setVisible(enabled)
        self.ocr_card.setVisible(not enabled)

    def is_capture_mode(self) -> bool:
        """当前是否为抓包模式"""
        return self.radio_capture.isChecked()

    def refresh_interfaces(self):
        """刷新网卡列表"""
        self.combo_interface.clear()
        try:
            ifs = list_interfaces()
            for guid, desc in ifs:
                self.combo_interface.addItem(desc, guid)
        except Exception as e:
            debug_log(f"刷新网卡失败: {e}")


class HealthBarWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._percent = 0.0
        self._is_dead = False
        self.setMinimumHeight(24)
        self.setMinimumWidth(200)

    def set_percent(self, percent: float):
        self._percent = max(0.0, min(100.0, percent))
        self._is_dead = (percent <= 0)
        self.update()

    def paintEvent(self, event):
        from PyQt5.QtGui import QPainter, QColor, QBrush, QPen, QFont
        from PyQt5.QtCore import Qt

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        bg_color = QColor(50, 50, 50) if isDarkTheme() else QColor(220, 220, 220)
        painter.setBrush(QBrush(bg_color))
        if self._is_dead:
            painter.setPen(QPen(QColor(231, 76, 60), 2))
        else:
            painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, w, h, 4, 4)

        if not self._is_dead and self._percent > 0:
            if self._percent > 60:
                bar_color = QColor(46, 204, 113)
            elif self._percent > 30:
                bar_color = QColor(243, 156, 18)
            else:
                bar_color = QColor(231, 76, 60)

            bar_width = int(w * self._percent / 100)
            painter.setBrush(QBrush(bar_color))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(0, 0, bar_width, h, 4, 4)

        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        if self._is_dead:
            text = "死亡"
            painter.setPen(QColor(231, 76, 60))
        else:
            text = f"{self._percent:.1f}%"
        painter.drawText(self.rect(), Qt.AlignCenter, text)


class HealthInterface(QWidget):
    clear_players_requested = pyqtSignal()
    self_name_changed = pyqtSignal(str)

    PAGE_SIZE = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HealthInterface")
        self.players = []
        self.self_health = None
        self.has_team_list = False
        self._current_page = 0
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        title = SubtitleLabel("玩家血量状态")
        title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Bold))
        layout.addWidget(title)

        tip_label = CaptionLabel("💡 若未检测到玩家则需切线")
        tip_label.setTextColor(QColor(255, 152, 0), QColor(255, 183, 77))
        tip_label.setFont(QFont("Microsoft YaHei UI", 10))
        layout.addWidget(tip_label)

        status_card = CardWidget(self)
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(20, 18, 20, 18)
        status_layout.setSpacing(10)

        status_header = QHBoxLayout()
        status_title = BodyLabel("检测状态")
        status_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        status_header.addWidget(status_title)
        status_header.addStretch()
        self.status_label = CaptionLabel("等待OCR启动...")
        self.status_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        status_header.addWidget(self.status_label)
        status_layout.addLayout(status_header)

        self.team_status_label = BodyLabel("队伍状态: 未检测")
        status_layout.addWidget(self.team_status_label)

        layout.addWidget(status_card)

        self_card = CardWidget(self)
        self_layout = QVBoxLayout(self_card)
        self_layout.setContentsMargins(20, 18, 20, 18)
        self_layout.setSpacing(12)

        self_title = BodyLabel("自身血量")
        self_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        self_layout.addWidget(self_title)

        self_info_layout = QHBoxLayout()
        self.self_name_label = BodyLabel("玩家: 未检测")
        self_info_layout.addWidget(self.self_name_label)
        self_info_layout.addStretch()
        self.self_hp_label = BodyLabel("0.0%")
        self_info_layout.addWidget(self.self_hp_label)
        self_layout.addLayout(self_info_layout)

        self.self_health_bar = HealthBarWidget()
        self_layout.addWidget(self.self_health_bar)

        self_name_row = QHBoxLayout()
        self_name_label = CaptionLabel("自身名称:")
        self_name_row.addWidget(self_name_label)
        self.self_name_input = LineEdit()
        self.self_name_input.setPlaceholderText("输入你的游戏角色名")
        self.self_name_input.setFixedHeight(28)
        self_name_row.addWidget(self.self_name_input, 1)
        self.set_self_name_btn = PrimaryPushButton("设置")
        self.set_self_name_btn.setFixedHeight(28)
        self.set_self_name_btn.clicked.connect(self._on_set_self_name)
        self_name_row.addWidget(self.set_self_name_btn)
        self_layout.addLayout(self_name_row)

        layout.addWidget(self_card)

        team_card = CardWidget(self)
        team_layout = QVBoxLayout(team_card)
        team_layout.setContentsMargins(20, 18, 20, 18)
        team_layout.setSpacing(12)

        team_header = QHBoxLayout()
        team_title = BodyLabel("队伍成员血量")
        team_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        team_header.addWidget(team_title)
        team_header.addStretch()
        self.clear_players_btn = PushButton("清空记录")
        self.clear_players_btn.setFixedHeight(28)
        self.clear_players_btn.clicked.connect(self._on_clear_players)
        team_header.addWidget(self.clear_players_btn)
        team_layout.addLayout(team_header)

        team_desc = CaptionLabel("检测到队伍列表时显示各成员血量")
        team_desc.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        team_layout.addWidget(team_desc)

        self.team_container = QVBoxLayout()
        self.team_container.setSpacing(10)
        team_layout.addLayout(self.team_container)

        self.no_team_label = CaptionLabel("未检测到队伍列表")
        self.no_team_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        self.team_container.addWidget(self.no_team_label)

        # 翻页控件
        self.pagination_row = QHBoxLayout()
        self.prev_page_btn = PushButton("上一页")
        self.prev_page_btn.setFixedHeight(28)
        self.prev_page_btn.clicked.connect(self._on_prev_page)
        self.pagination_row.addWidget(self.prev_page_btn)
        self.pagination_row.addStretch()
        self.page_label = CaptionLabel("第 1/1 页")
        self.pagination_row.addWidget(self.page_label)
        self.pagination_row.addStretch()
        self.next_page_btn = PushButton("下一页")
        self.next_page_btn.setFixedHeight(28)
        self.next_page_btn.clicked.connect(self._on_next_page)
        self.pagination_row.addWidget(self.next_page_btn)
        team_layout.addLayout(self.pagination_row)

        layout.addWidget(team_card)
        layout.addStretch()

    def update_health(self, self_health, players, has_team_list):
        self.self_health = self_health
        self.players = players
        self.has_team_list = has_team_list

        if self_health:
            if self_health.name:
                self.self_name_label.setText(f"玩家: {self_health.name}")
            self.self_hp_label.setText(f"{self_health.health_percent:.1f}%")
            self.self_health_bar.set_percent(self_health.health_percent)

        if has_team_list:
            self.team_status_label.setText("队伍状态: 已检测到队伍列表")
            self.status_label.setText("OCR运行中 | 队伍模式")
        else:
            self.team_status_label.setText("队伍状态: 未检测到队伍列表")
            self.status_label.setText("OCR运行中 | 单人模式")

        self._update_team_list()

    def _on_set_self_name(self):
        name = self.self_name_input.text().strip()
        if name:
            self.self_name_changed.emit(name)

    def _on_clear_players(self):
        self.players = []
        self.has_team_list = False
        self._update_team_list()
        self.clear_players_requested.emit()

    def _clear_team_list(self):
        while self.team_container.count():
            child = self.team_container.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _update_team_list(self):
        self._clear_team_list()

        if not self.has_team_list or not self.players:
            self.no_team_label = CaptionLabel("未检测到队伍列表")
            self.no_team_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
            self.team_container.addWidget(self.no_team_label)
            self.prev_page_btn.setEnabled(False)
            self.next_page_btn.setEnabled(False)
            self.page_label.setText("第 1/1 页")
            return

        total = len(self.players)
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self._current_page >= total_pages:
            self._current_page = total_pages - 1

        start = self._current_page * self.PAGE_SIZE
        end = min(start + self.PAGE_SIZE, total)
        page_players = self.players[start:end]

        for i, player in enumerate(page_players):
            global_idx = start + i
            player_card = QWidget()
            player_layout = QVBoxLayout(player_card)
            player_layout.setContentsMargins(0, 0, 0, 0)
            player_layout.setSpacing(6)

            info_row = QHBoxLayout()
            name_label = BodyLabel(f"{global_idx + 1}. {player.name}")
            info_row.addWidget(name_label)
            info_row.addStretch()
            hp_label = CaptionLabel(f"{player.health_percent:.1f}%")
            info_row.addWidget(hp_label)
            player_layout.addLayout(info_row)

            bar = HealthBarWidget()
            bar.set_percent(player.health_percent)
            bar.setMinimumHeight(20)
            player_layout.addWidget(bar)

            self.team_container.addWidget(player_card)

        # 更新翻页状态
        self.prev_page_btn.setEnabled(self._current_page > 0)
        self.next_page_btn.setEnabled(self._current_page < total_pages - 1)
        self.page_label.setText(f"第 {self._current_page + 1}/{total_pages} 页")

    def _on_prev_page(self):
        if self._current_page > 0:
            self._current_page -= 1
            self._update_team_list()

    def _on_next_page(self):
        total = len(self.players) if self.players else 0
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self._current_page < total_pages - 1:
            self._current_page += 1
            self._update_team_list()

    def set_ocr_status(self, running: bool):
        if running:
            self.status_label.setText("OCR运行中")
            self.status_label.setTextColor(QColor(46, 204, 113), QColor(46, 204, 113))
        else:
            self.status_label.setText("OCR未启动")
            self.status_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))

    def set_capture_status(self, running: bool):
        if running:
            self.status_label.setText("抓包运行中")
            self.status_label.setTextColor(QColor(46, 204, 113), QColor(46, 204, 113))
        else:
            self.status_label.setText("抓包未启动")
            self.status_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))


# 强度解读方式映射
STRENGTH_MODE_MAP = {
    "不变": 0b00,
    "增加": 0b01,
    "减少": 0b10,
    "绝对设置": 0b11,
}




class DGLabDebugInterface(QWidget):
    """郊狼控制页面"""


    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DGLabDebugInterface")
        self.init_ui()

    def init_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        content = QWidget()
        content.setObjectName("dglabDebugContent")
        content.setMinimumWidth(510)
        content.setStyleSheet("#dglabDebugContent { background: #202020; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = SubtitleLabel("郊狼控制")
        title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Bold))
        layout.addWidget(title)



        # === 郊狼设备控制（内置客户端，无外部依赖）===
        hub_card = CardWidget(content)
        hub_layout = QVBoxLayout(hub_card)
        hub_layout.setContentsMargins(14, 10, 14, 10)
        hub_layout.setSpacing(6)

        _LBL = "QLabel { color: #d4d4d4 !important; background: transparent; font-size: 10px; padding: 0; margin: 0; }"
        _LBL_SMALL = "QLabel { color: #b0b0b0 !important; background: transparent; font-size: 10px; padding: 0; margin: 0; }"
        _LBL_TITLE = "QLabel { color: #f0f0f0 !important; background: transparent; font-size: 12px; font-weight: bold; padding: 0; margin: 0; }"
        _LBL_WARN = "QLabel { color: #e0b070 !important; background: transparent; font-size: 11px; font-weight: bold; padding: 0; margin: 0; }"
        _LBL_INFO = "QLabel { color: #70b0e0 !important; background: transparent; font-size: 11px; font-weight: bold; padding: 0; margin: 0; }"
        _BTN_QSS = (
            "QPushButton { padding: 0 4px; margin: 0; font-size: 10px; }"
        )

        # ── 标题行 ──
        hub_title_h = QHBoxLayout()
        hub_title_h.setSpacing(6)
        hub_title = QLabel("郊狼设备控制")
        hub_title.setStyleSheet(_LBL_TITLE)
        hub_title_h.addWidget(hub_title)
        hub_title_h.addStretch()
        self.btn_hub_toggle = PushButton("启动服务器")
        self.btn_hub_toggle.setFixedHeight(24)
        self.btn_hub_toggle.setMinimumWidth(0)
        self.btn_hub_toggle.adjustSize()
        self.btn_hub_toggle.setFixedSize(
            max(80, self.btn_hub_toggle.sizeHint().width() + 12), 24)
        self.btn_hub_toggle.setStyleSheet(_BTN_QSS)
        self.btn_hub_toggle.clicked.connect(self._on_hub_toggle)
        hub_title_h.addWidget(self.btn_hub_toggle)
        hub_layout.addLayout(hub_title_h)

        self.hub_status_label = QLabel("服务器：未启动")
        self.hub_status_label.setStyleSheet(_LBL_SMALL)
        hub_layout.addWidget(self.hub_status_label)

        # ── 连接实机：二维码（左）+ 连接 URL（右）────────
        qr_row = QHBoxLayout()
        qr_row.setSpacing(8)

        qr_col = QVBoxLayout()
        qr_col.setSpacing(2)
        lbl_qr = QLabel("扫码连接 DG-Lab APP：")
        lbl_qr.setStyleSheet(_LBL_SMALL)
        qr_col.addWidget(lbl_qr)
        self.hub_qr_label = QLabel()
        self.hub_qr_label.setMinimumSize(100, 100)
        self.hub_qr_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.hub_qr_label.setAlignment(Qt.AlignCenter)
        self.hub_qr_label.setStyleSheet(
            "QLabel { background: #ffffff; border: 1px solid #444; border-radius: 3px; padding: 0; margin: 0; }")
        qr_col.addWidget(self.hub_qr_label)
        qr_row.addLayout(qr_col)

        url_col = QVBoxLayout()
        url_col.setSpacing(2)
        lbl_url = QLabel("WebSocket 连接URL：")
        lbl_url.setStyleSheet(_LBL_SMALL)
        url_col.addWidget(lbl_url)
        self.hub_url_text = TextEdit()
        self.hub_url_text.setReadOnly(True)
        self.hub_url_text.setFixedHeight(72)
        self.hub_url_text.setStyleSheet(
            "QTextEdit { background: #1a1a1a; color: #cccccc !important; padding: 2px; "
            "font-family: Consolas, monospace; font-size: 10px; border: 1px solid #333; }")
        url_col.addWidget(self.hub_url_text)
        qr_row.addLayout(url_col, 1)

        hub_layout.addLayout(qr_row)

        _LINE_QSS = (
            "QLineEdit { background: #1a1a1a; color: #cccccc !important; padding: 0 6px; margin: 0; "
            "border: 1px solid #3a3a3a; border-radius: 3px; font-size: 11px; selection-background-color: #0a84ff; }"
            "QLineEdit:focus { border: 1px solid #0a84ff; }"
        )

        # 强制所有字段标签固定窄宽度，消除冒号后视觉空白
        def _tight(widget, w):
            widget.setFixedWidth(w)
            widget.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        def _num_edit(default, lo, hi, width=44):
            ed = QLineEdit(str(default))
            ed.setStyleSheet(_LINE_QSS)
            ed.setFixedHeight(28)
            ed.setFixedWidth(width)
            ed.setValidator(QIntValidator(lo, hi, ed))
            ed.setAlignment(Qt.AlignCenter)
            return ed

        # ── 强度实时曲线（替换旧的波形预览） ──
        self.hub_pulse_preview = QLabel()
        self.hub_pulse_preview.setFixedHeight(76)
        self.hub_pulse_preview.setAlignment(Qt.AlignCenter)
        self.hub_pulse_preview.setStyleSheet(
            "QLabel { background: #1a1a1a; border: 1px solid #333; border-radius: 3px; color: #b0b0b0; padding: 0; margin: 0; }")
        hub_layout.addWidget(self.hub_pulse_preview)
        # 双通道时间序列缓冲（最近 120 个采样点，≈60s @ 500ms 刷新）
        self._strength_hist_a: list[int] = [0] * 120
        self._strength_hist_b: list[int] = [0] * 120

        # ── 强度上限 + 当前强度 ──
        limit_row = QHBoxLayout()
        limit_row.setSpacing(2)
        lbl_limit = QLabel("强度上限:")
        lbl_limit.setStyleSheet(_LBL)
        _tight(lbl_limit, 60)
        limit_row.addWidget(lbl_limit)
        self.hub_spin_limit = _num_edit(200, 1, 200, 62)
        self.hub_spin_limit.textChanged.connect(self._on_limit_changed)
        limit_row.addWidget(self.hub_spin_limit)
        lbl_limit_r = QLabel("(0-200)")
        lbl_limit_r.setStyleSheet(_LBL_SMALL)
        _tight(lbl_limit_r, 44)
        limit_row.addWidget(lbl_limit_r)
        limit_row.addStretch()
        self.hub_lbl_current = QLabel("当前: A=0 B=0")
        self.hub_lbl_current.setStyleSheet(_LBL)
        _tight(self.hub_lbl_current, 78)
        limit_row.addWidget(self.hub_lbl_current)
        hub_layout.addLayout(limit_row)

        # ── 一键开火 ──
        fire_row = QHBoxLayout()
        fire_row.setSpacing(2)
        lbl_fire = QLabel("开火:")
        lbl_fire.setStyleSheet(_LBL)
        _tight(lbl_fire, 34)
        fire_row.addWidget(lbl_fire)
        self.hub_spin_fire_str = _num_edit(15, 1, 50, 46)
        fire_row.addWidget(self.hub_spin_fire_str)
        lbl_str = QLabel("强度")
        lbl_str.setStyleSheet(_LBL)
        _tight(lbl_str, 26)
        fire_row.addWidget(lbl_str)
        self.hub_spin_fire_ms = _num_edit(10000, 100, 60000, 60)
        fire_row.addWidget(self.hub_spin_fire_ms)
        lbl_ms = QLabel("ms")
        lbl_ms.setStyleSheet(_LBL)
        _tight(lbl_ms, 18)
        fire_row.addWidget(lbl_ms)
        self.hub_btn_fire = PrimaryPushButton("开火!")
        self.hub_btn_fire.setFixedHeight(28)
        self.hub_btn_fire.setFixedWidth(60)
        self.hub_btn_fire.setStyleSheet(
            "QPushButton { padding: 0 4px; margin: 0; font-size: 10px; font-weight: bold; }")
        self.hub_btn_fire.clicked.connect(self._on_fire_clicked)
        fire_row.addWidget(self.hub_btn_fire)
        hub_layout.addLayout(fire_row)

        # ── 空闲波形循环播放 ──
        idle_title = QLabel("空闲波形播放（未触发开火时）")
        idle_title.setStyleSheet(_LBL_TITLE)
        hub_layout.addWidget(idle_title)
        # 初始化阶段标志：避免创建控件时的信号立刻触发 ws 配置同步（setChecked/addItem 都会发信号）
        self._idle_ui_initing = True

        idle_row = QHBoxLayout()
        idle_row.setSpacing(2)
        lbl_idle = QLabel("启用:")
        lbl_idle.setStyleSheet(_LBL)
        _tight(lbl_idle, 34)
        idle_row.addWidget(lbl_idle)
        self.hub_switch_idle_wave = SwitchButton()
        self.hub_switch_idle_wave.setChecked(False)
        self.hub_switch_idle_wave.checkedChanged.connect(self._on_idle_wave_toggled)
        idle_row.addWidget(self.hub_switch_idle_wave)
        lbl_idle_str = QLabel("强度:")
        lbl_idle_str.setStyleSheet(_LBL)
        _tight(lbl_idle_str, 34)
        idle_row.addWidget(lbl_idle_str)
        self.hub_spin_idle_strength = _num_edit(20, 1, 200, 46)
        self.hub_spin_idle_strength.textChanged.connect(self._on_idle_strength_changed)
        idle_row.addWidget(self.hub_spin_idle_strength)
        lbl_idle_pulse = QLabel("波形:")
        lbl_idle_pulse.setStyleSheet(_LBL)
        _tight(lbl_idle_pulse, 34)
        idle_row.addWidget(lbl_idle_pulse)
        self.hub_combo_idle_pulse = ComboBox()
        self.hub_combo_idle_pulse.setFixedHeight(28)
        self.hub_combo_idle_pulse.setMinimumWidth(110)
        self._init_idle_pulse_combo()
        self.hub_combo_idle_pulse.currentIndexChanged.connect(self._on_idle_pulse_changed)
        idle_row.addWidget(self.hub_combo_idle_pulse)
        idle_row.addStretch()
        hub_layout.addLayout(idle_row)
        # 控件都创建 + 信号 connect 完后，清除初始化标志，并做一次强制同步（保证默认选择立刻生效）
        self._idle_ui_initing = False
        # ⚠️ 初始化完成后立刻同步一次：避免"用户没动过下拉就用不到选择的波形"问题
        try:
            self._apply_idle_wave_config()
        except Exception as e:
            import traceback
            try:
                self.debug_log(f"[初始化] 空闲波形配置首次同步失败(可忽略): {e}")
            except Exception:
                import sys
                sys.stderr.write(
                    f"[初始化] 空闲波形首次同步失败: {e}\n{traceback.format_exc()}\n")

        # ── 星痕共鸣事件统计 ──
        # === 星痕共鸣事件触发卡片 ===
        # 强度阶梯参数（默认值可在 UI 输入框中全局修改）
        self.FIRE_INITIAL = 15      # 初始开火强度
        self.FIRE_DEFAULT_MS = 10000  # 默认持续时间 10 秒
        # 初始值：下次触发强度 = 初始值
        self._next_fire_strength = self.FIRE_INITIAL

        evt_card = CardWidget(content)
        evt_layout = QVBoxLayout(evt_card)
        evt_layout.setContentsMargins(12, 10, 12, 10)
        evt_layout.setSpacing(6)

        evt_title = QLabel("星痕共鸣事件触发")
        evt_title.setStyleSheet(_LBL_TITLE)
        hub_layout.addWidget(evt_title)

        stat_row = QHBoxLayout()
        stat_row.setSpacing(6)
        self.hub_lbl_self_deaths = QLabel("自己死亡:0")
        self.hub_lbl_self_deaths.setStyleSheet(_LBL_WARN)
        _tight(self.hub_lbl_self_deaths, 70)
        stat_row.addWidget(self.hub_lbl_self_deaths)
        self.hub_lbl_mate_deaths = QLabel("队友死亡:0")
        self.hub_lbl_mate_deaths.setStyleSheet(_LBL_INFO)
        _tight(self.hub_lbl_mate_deaths, 70)
        stat_row.addWidget(self.hub_lbl_mate_deaths)
        stat_row.addStretch()
        # 下次触发强度显示
        self.hub_lbl_next_fire = QLabel(f"下次强度: {self.FIRE_INITIAL}")
        self.hub_lbl_next_fire.setStyleSheet(
            "QLabel { color: #ffcc80 !important; background: transparent;"
            " font-size: 11px; font-weight: bold; padding: 0; margin: 0; }")
        _tight(self.hub_lbl_next_fire, 100)
        stat_row.addWidget(self.hub_lbl_next_fire)
        hub_layout.addLayout(stat_row)

        # 触发条件 + 每次死亡增量 + 全局强度上限
        trig_row = QHBoxLayout()
        trig_row.setSpacing(2)
        lbl_cond = QLabel("条件:")
        lbl_cond.setStyleSheet(_LBL)
        _tight(lbl_cond, 34)
        trig_row.addWidget(lbl_cond)
        self.hub_combo_cond = ComboBox()
        self.hub_combo_cond.setFixedHeight(28)
        self.hub_combo_cond.setMinimumWidth(96)
        self.hub_combo_cond.addItems(["自己死亡", "队友死亡", "任意死亡", "血量<30%"])
        trig_row.addWidget(self.hub_combo_cond)
        lbl_incr = QLabel("增量:")
        lbl_incr.setStyleSheet(_LBL)
        _tight(lbl_incr, 34)
        trig_row.addWidget(lbl_incr)
        self.hub_spin_trig_val = _num_edit(5, 1, 40, 38)
        trig_row.addWidget(self.hub_spin_trig_val)
        lbl_cap = QLabel("上限:")
        lbl_cap.setStyleSheet(_LBL)
        _tight(lbl_cap, 34)
        trig_row.addWidget(lbl_cap)
        self.hub_spin_fire_cap = _num_edit(50, 1, 200, 42)
        # 全局强度上限变化时，立刻把当前下次强度夹到新上限以内
        self.hub_spin_fire_cap.textChanged.connect(self._on_fire_cap_changed)
        trig_row.addWidget(self.hub_spin_fire_cap)
        self.hub_switch_trig = SwitchButton()
        self.hub_switch_trig.setChecked(False)
        trig_row.addWidget(self.hub_switch_trig)
        hub_layout.addLayout(trig_row)

        # 设备事件日志
        self.hub_devlog_text = TextEdit()
        self.hub_devlog_text.setReadOnly(True)
        self.hub_devlog_text.setFixedHeight(68)
        self.hub_devlog_text.setStyleSheet(
            "QTextEdit { background: #1a1a1a; color: #cccccc !important; "
            "font-family: Consolas, monospace; font-size: 10px; border: 1px solid #333; }"
        )
        hub_layout.addWidget(self.hub_devlog_text)

        layout.addWidget(hub_card)

        # WS 日志同步游标（避免重复显示旧日志）
        self._last_ws_log_len = 0

        # 定时刷新郊狼面板 + 事件触发检查（500ms）
        self._mon_timer = QTimer(self)
        self._mon_timer.timeout.connect(self._refresh_mon_panel)
        self._mon_timer.timeout.connect(self._check_event_triggers)
        self._mon_timer.start(500)

        # === 日志卡片 ===
        log_card = CardWidget(content)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(12, 10, 12, 10)
        log_layout.setSpacing(6)

        log_header = QHBoxLayout()
        log_title = QLabel("通信日志")
        log_title.setStyleSheet(
            "QLabel { color: #f0f0f0 !important; font-size: 12px; font-weight: bold; background: transparent; }")
        log_header.addWidget(log_title)
        log_header.addStretch()
        self.btn_clear_log = PushButton("清空日志")
        self.btn_clear_log.setFixedHeight(24)
        self.btn_clear_log.setFixedWidth(72)
        self.btn_clear_log.setStyleSheet(
            "QPushButton { padding: 0 4px; margin: 0; font-size: 10px; }")
        log_header.addWidget(self.btn_clear_log)
        log_layout.addLayout(log_header)

        self.log_text = TextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFixedHeight(200)
        self.log_text.setStyleSheet(
            "QTextEdit { background: #1a1a1a; color: #cccccc !important; "
            "font-family: Consolas, monospace; font-size: 10px; border: 1px solid #333; }")
        log_layout.addWidget(self.log_text)
        self.btn_clear_log.clicked.connect(self.log_text.clear)

        layout.addWidget(log_card)
        layout.addStretch()

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

    # ==================== 共享内存测试面板 ====================
    def debug_log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        # 郊狼控制页面使用 hub_devlog_text（唯一的日志框），其他页面用 log_text
        log_widget = getattr(self, "hub_devlog_text", None)
        if log_widget is None:
            log_widget = getattr(self, "log_text", None)
        if log_widget is None:
            # 极端兜底：还没创建任何日志框时，只写 stderr（避免初始化阶段 AttributeError）
            import sys
            sys.stderr.write(f"[debug_log][{timestamp}] {msg}\n")
            sys.stderr.flush()
            return
        try:
            log_widget.append(f"[{timestamp}] {msg}")
            vsb = log_widget.verticalScrollBar()
            vsb.setValue(vsb.maximum())
        except Exception:
            pass

    # ==================== 郊狼设备控制面板 ====================
    def _on_hub_toggle(self):
        """启动/停止内置 Coyote Game Hub（WebSocket + HTTP 共用端口 8920）。

        WS 服务器作为主服务监听 8920，HTTP 请求通过 process_request 钩子转发到
        coyote_http_server.handle_http_request 处理。这样 DG-Lab APP 扫码后能
        通过 WebSocket 连接到本程序，同时外部 HTTP 客户端也能访问 REST API。
        """
        import coyote_ws_server
        import coyote_http_server
        import coyote_device

        ws_mgr = coyote_ws_server.get_default_ws_manager()
        http_srv = coyote_http_server.get_default_server()
        mgr = coyote_device.get_default_manager()

        if ws_mgr.running:
            ws_mgr.stop()
            http_srv.ws_manager = None
            self.btn_hub_toggle.setText("启动服务器")
            self.debug_log("[CoyoteHub] WebSocket + HTTP 服务器已停止")
            self.hub_qr_label.clear()
            self.hub_url_text.setPlainText("")
            return

        def _http_handler(method, path, headers, body):
            return coyote_http_server.handle_http_request(
                method, path, headers, body,
                manager=mgr, ws_manager=ws_mgr, port=ws_mgr.port)

        ws_mgr.set_http_handler(_http_handler)
        http_srv.ws_manager = ws_mgr

        def _on_ws_connected(ws_client):
            try:
                dev = coyote_device.CoyoteClient(
                    client_id=ws_client.client_id,
                    name=f"DG-Lab设备{ws_client.target_id[:8]}",
                    mock=False,
                )
                dev.ws_client_id = ws_client.client_id
                dev.connected = True
                dev.battery = 100
                dev.signal = 100
                dev.strength_limit = min(ws_client.limit_a or 200, 200)
                mgr.add_client(dev)
                self.debug_log(
                    f"[CoyoteHub] 真机已连接: {ws_client.client_id[:8]}… → "
                    f"{ws_client.target_id[:8]}…")
            except Exception as e:
                self.debug_log(f"[CoyoteHub] 注册真机失败: {e}")

        def _on_ws_disconnected(ws_client):
            try:
                mgr.remove_client(ws_client.client_id)
                self.debug_log(f"[CoyoteHub] 真机已断开: {ws_client.client_id[:8]}…")
            except Exception as e:
                self.debug_log(f"[CoyoteHub] 移除真机失败: {e}")

        ws_mgr.set_callbacks(
            on_connected=_on_ws_connected,
            on_disconnected=_on_ws_disconnected,
        )

        ok = ws_mgr.start(host="0.0.0.0", port=8920)
        if ok:
            self.btn_hub_toggle.setText("停止服务器")
            self.debug_log(
                f"[CoyoteHub] WebSocket 服务已启动: ws://0.0.0.0:8920 "
                f"(HTTP 请求转发到内置处理器)")
            self._refresh_hub_qr()
        else:
            self.btn_hub_toggle.setText("启动服务器")
            QMessageBox.warning(
                self, "启动失败",
                "Coyote Game Hub 启动失败（端口 8920 被占用？）\n"
                "请检查是否有其他程序占用此端口。")

    def _refresh_hub_qr(self):
        """刷新 DG-Lab APP 扫码二维码和连接URL列表。"""
        try:
            import coyote_ws_server
            ws_mgr = coyote_ws_server.get_default_ws_manager()

            if not ws_mgr.running:
                self.hub_qr_label.clear()
                self.hub_url_text.setPlainText("")
                return

            urls = ws_mgr.get_connect_urls()
            url_lines = []
            for u in urls:
                url_lines.append(f"[{u['domain']}]")
                url_lines.append(u['connectUrl'])
                url_lines.append("")
            self.hub_url_text.setPlainText("\n".join(url_lines))

            if urls:
                try:
                    import qrcode
                    from io import BytesIO
                    qr = qrcode.QRCode(
                        version=None,
                        error_correction=qrcode.constants.ERROR_CORRECT_L,
                        box_size=4,
                        border=1,
                    )
                    qr.add_data(urls[0]["connectUrl"])
                    qr.make(fit=True)
                    pil_img = qr.make_image(fill_color="black", back_color="white")
                    buf = BytesIO()
                    pil_img.save(buf, format="PNG")
                    pixmap = QPixmap()
                    pixmap.loadFromData(buf.getvalue(), "PNG")
                    target_size = self.hub_qr_label.contentsRect().size()
                    self.hub_qr_label.setPixmap(
                        pixmap.scaled(target_size, Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation))
                except Exception as e:
                    self.hub_qr_label.setText(f"二维码失败: {e}")
        except Exception as e:
            self.debug_log(f"[CoyoteHub] 刷新二维码失败: {e}")

    def _on_limit_changed(self, text: str):
        """强度上限（通道硬件上限 0-200）变化时，同步到所有设备。"""
        import coyote_device
        try:
            value = int(text)
        except (ValueError, TypeError):
            return
        mgr = coyote_device.get_default_manager()
        mgr.set_all_strength_limit(value)
        self.debug_log(f"[Coyote] 通道强度上限设为 {value}")

    # ── 空闲波形播放控制（唯一的波形选择入口：同时作为默认开火波形） ──
    def _init_idle_pulse_combo(self):
        """填充波形下拉框（内置 18 个 + 已导入波形），作为页面唯一的波形选择。"""
        import coyote_device
        self.hub_combo_idle_pulse.blockSignals(True)
        try:
            self.hub_combo_idle_pulse.clear()
            for p in coyote_device.BUILTIN_PULSES:
                self.hub_combo_idle_pulse.addItem(f"[内置] {p['name']}", p["id"])
            imported = coyote_device.all_pulses()[len(coyote_device.BUILTIN_PULSES):]
            for p in imported:
                self.hub_combo_idle_pulse.addItem(f"[导入] {p['name']}", p["id"])
            # 默认选呼吸（d6f83af0）
            idx = self.hub_combo_idle_pulse.findData("d6f83af0")
            if idx >= 0:
                self.hub_combo_idle_pulse.setCurrentIndex(idx)
        finally:
            self.hub_combo_idle_pulse.blockSignals(False)

    def _apply_idle_wave_config(self):
        """把当前 UI 配置同步到 WSManager：空闲播放 + 默认开火共用同一个 waveform。"""
        # UI 初始化阶段（控件还在创建/connect）直接跳过，避免无谓同步
        if getattr(self, "_idle_ui_initing", False):
            return
        import coyote_ws_server
        import coyote_device
        ws_mgr = coyote_ws_server.get_default_ws_manager()
        enabled = self.hub_switch_idle_wave.isChecked()
        try:
            strength = int(self.hub_spin_idle_strength.text())
        except (ValueError, TypeError):
            strength = 20
        strength = max(1, min(200, strength))
        pulse_id = self.hub_combo_idle_pulse.currentData()
        if pulse_id is None:
            pulse_id = "d6f83af0"

        # 同时也把这个波形设为设备默认开火波形（保证唯一的下拉选择一定生效）
        try:
            coyote_device.get_default_manager().set_all_pulse(pulse_id)
        except Exception as e:
            # 不抛异常避免中断 UI，用 self.debug_log 上报即可
            try:
                self.debug_log(f"[波形] set_all_pulse 失败(可忽略): {e}")
            except Exception:
                pass

        pulse_name = self.hub_combo_idle_pulse.currentText() or pulse_id
        self.debug_log(
            f"[Coyote] 波形配置同步: 空闲={'开' if enabled else '关'} "
            f"强度={strength} 波形={pulse_name}({pulse_id})")

        ws_mgr.set_idle_wave_config(enabled, strength, pulse_id)

    def _on_idle_wave_toggled(self, checked: bool):
        """空闲波形开关切换。"""
        self.debug_log(f"[Coyote] 空闲波形播放: {'开启' if checked else '关闭'}")
        self._apply_idle_wave_config()

    def _on_idle_strength_changed(self, text: str):
        """空闲播放强度变化。"""
        self._apply_idle_wave_config()

    def _on_idle_pulse_changed(self, index: int):
        """空闲播放波形变化。"""
        self._apply_idle_wave_config()

    def _on_fire_cap_changed(self, text: str):
        """星痕共鸣全局强度上限变化：立即把下次触发强度夹到新上限以内。"""
        try:
            cap = int(text)
        except (ValueError, TypeError):
            return
        # 夹到合法范围（1-200，与通道上限保持一致）
        cap = max(1, min(200, cap))
        self._next_fire_strength = max(
            self.FIRE_INITIAL, min(cap, self._next_fire_strength))
        self.hub_lbl_next_fire.setText(f"下次强度: {self._next_fire_strength}")

    def _on_fire_clicked(self):
        """一键开火：以当前输入的强度/时长立即 fire。

        DG-Lab APP 端对 socket 下发的 set_strength(OP_SET_TO) 有限制：
        - 如果 APP 通道强度上限（用户在强度滑条上方的那个 limit）小于目标强度，
          会被 APP 内部 clamp 到 limit（甚至 0），导致「实际上无强度输出」。
        - 这也是为什么"需要用户先在 APP 上手动把强度滑条拉上去"的根本原因：
          只有硬件 limit >= fire_strength 时，socket 下发的 OP_SET_TO 才会真正
          驱动硬件输出。
        """
        import coyote_device
        try:
            strength = int(self.hub_spin_fire_str.text())
            time_ms = int(self.hub_spin_fire_ms.text())
        except (ValueError, TypeError):
            return
        mgr = coyote_device.get_default_manager()
        ok = mgr.broadcast_fire("all", strength=strength, time_ms=time_ms, override=True)
        self.debug_log(f"[Coyote] 开火: 强度={strength} 时长={time_ms}ms → {len(ok)} 设备")
        if ok:
            self.debug_log(
                f"[Coyote] 提示: 若设备无输出，请在 DG-Lab APP 把通道强度上限拉到 >= {strength}")

    def _draw_strength_curve(self, cur_a: int, cur_b: int):
        """绘制 A/B 双通道强度时间序列到 hub_pulse_preview（QLabel + QPixmap 自绘）。"""
        from PyQt5.QtGui import QPixmap, QPainter, QPen, QColor, QFont
        # 1) 把最新采样推进环形缓冲
        buf = getattr(self, "_strength_hist_a", None)
        if buf is None:
            self._strength_hist_a = [0] * 120
            self._strength_hist_b = [0] * 120
        self._strength_hist_a.pop(0)
        self._strength_hist_a.append(max(0, min(200, int(cur_a))))
        self._strength_hist_b.pop(0)
        self._strength_hist_b.append(max(0, min(200, int(cur_b))))

        lbl = self.hub_pulse_preview
        w = max(360, lbl.width())
        h = max(60, lbl.height())
        pm = QPixmap(w, h)
        pm.fill(QColor("#121212"))
        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            # 2) 背景网格（25/50/75/100%）
            grid_pen = QPen(QColor("#2a2a2a"), 1)
            p.setPen(grid_pen)
            for i in range(1, 5):
                y = h - 12 - i * (h - 22) // 4
                p.drawLine(28, y, w - 4, y)
            for i in range(1, 6):
                x = 28 + i * (w - 32) // 6
                p.drawLine(x, 4, x, h - 14)

            # 3) Y 轴刻度 + 最大值标注（按 200 上限）
            lbl_font = QFont()
            lbl_font.setPixelSize(9)
            p.setFont(lbl_font)
            p.setPen(QPen(QColor("#888888"), 1))
            scale_max = 200
            for tick in (0, 50, 100, 150, 200):
                ratio = tick / scale_max if scale_max else 0
                y = h - 12 - int(ratio * (h - 22))
                p.drawText(2, y + 3, f"{tick:>3}")

            # 4) 绘制两条曲线：A=橙，B=青
            N = len(self._strength_hist_a)
            x_start = 28
            x_end = w - 4
            def _pt(i: int, val: int):
                x = x_start + int(i * (x_end - x_start) / max(1, N - 1))
                y = h - 12 - int(max(0, min(scale_max, val)) / scale_max * (h - 22))
                return x, y

            def _plot(series: list[int], color: str):
                p.setPen(QPen(QColor(color), 2))
                prev = None
                for i, v in enumerate(series):
                    pt = _pt(i, v)
                    if prev is None:
                        p.drawPoint(pt[0], pt[1])
                    else:
                        p.drawLine(prev[0], prev[1], pt[0], pt[1])
                    prev = pt

            _plot(self._strength_hist_a, "#FF8A3D")   # 通道A：橙
            _plot(self._strength_hist_b, "#4DD0E1")   # 通道B：青

            # 5) 右上角图例 + 最新值
            p.setPen(QPen(QColor("#cccccc"), 1))
            p.drawText(w - 170, 12, "A 通道")
            p.setPen(QPen(QColor("#FF8A3D"), 3))
            p.drawLine(w - 204, 8, w - 176, 8)
            p.setPen(QPen(QColor("#cccccc"), 1))
            p.drawText(w - 90, 12, "B 通道")
            p.setPen(QPen(QColor("#4DD0E1"), 3))
            p.drawLine(w - 124, 8, w - 96, 8)

            now_val_txt = f"A={cur_a}  B={cur_b}"
            p.setPen(QPen(QColor("#e0e0e0"), 1))
            p.drawText(32, 12, now_val_txt)
        finally:
            p.end()
        lbl.setPixmap(pm)

    def _refresh_mon_panel(self):
        """定时刷新郊狼控制面板状态（服务器/设备/死亡统计/日志/强度曲线）。"""
        import coyote_ws_server
        import coyote_device
        import game_value_monitor
        try:
            ws_mgr = coyote_ws_server.get_default_ws_manager()
            mgr = coyote_device.get_default_manager()
            monitor = game_value_monitor.get_default_monitor()

            srv_state = (
                f"服务器：运行中(ws://:{ws_mgr.port}, {ws_mgr.client_count}个APP)"
                if ws_mgr.running else "服务器：未启动")
            clients = mgr.list_clients()
            dev_state = "无设备"
            cur_a = cur_b = 0
            if clients:
                c = clients[0]
                dev_state = (
                    f"{c['name']}  电={c['battery']}%  "
                    f"上限={c['limit']}  {'开火中' if c['firing'] else '待机'}")
                cur_a = c['strengthA']
                cur_b = c['strengthB']
            self.hub_status_label.setText(f"{srv_state}  |  {dev_state}")
            self.hub_lbl_current.setText(f"当前: A={cur_a} B={cur_b}")

            # 强度曲线重绘（每次刷新都采样+绘制）
            try:
                self._draw_strength_curve(cur_a, cur_b)
            except Exception as e:
                # 绘制异常不影响轮询主逻辑，self.debug_log 已做 widget 存在性兜底
                try:
                    self.debug_log(f"[强度曲线] 绘制异常(可忽略): {e}")
                except Exception:
                    pass

            self._refresh_hub_qr()

            if ws_mgr.running:
                self.btn_hub_toggle.setText("停止服务器")
            else:
                self.btn_hub_toggle.setText("启动服务器")

            stats = monitor.get_death_stats()
            self.hub_lbl_self_deaths.setText(f"自己死亡:{stats['self_deaths']}")
            self.hub_lbl_mate_deaths.setText(f"队友死亡:{stats['mate_deaths']}")
            devlogs = mgr.logs[-10:]
            dl_lines = [f"{ts}  [{typ}] {cid[:8]}…  {detail}"
                        for ts, typ, cid, detail in devlogs]
            self.hub_devlog_text.setPlainText("\n".join(dl_lines) or "(无事件)")

            # 同步 WS 层的新日志到通信日志面板（500ms 轮询一次）
            ws_logs = ws_mgr.logs
            if len(ws_logs) > self._last_ws_log_len:
                new_logs = ws_logs[self._last_ws_log_len:]
                for ts, msg in new_logs:
                    self.debug_log(f"[WS] {ts} {msg}")
                self._last_ws_log_len = len(ws_logs)
        except Exception as e:
            self.hub_status_label.setText(f"状态：异常 {e}")

    def _check_event_triggers(self):
        """星痕共鸣死亡触发：每次符合条件的死亡 → 立即用『下次触发强度』fire → 之后再累加到下次。

        规则（和 UI 对应）：
          - 初始『下次触发强度』= FIRE_INITIAL (15)
          - 每次死亡：先按显示值 fire（持续 FIRE_DEFAULT_MS = 10s），
            然后『下次触发强度』= 显示值 + 增量（默认+5），全局强度上限夹值。
        """
        import game_value_monitor
        import coyote_device
        if not self.hub_switch_trig.isChecked():
            return
        monitor = game_value_monitor.get_default_monitor()
        stats = monitor.get_death_stats()
        cond = self.hub_combo_cond.currentText()

        try:
            increment = int(self.hub_spin_trig_val.text())
        except (ValueError, TypeError):
            increment = 5
        try:
            cap = int(self.hub_spin_fire_cap.text())
        except (ValueError, TypeError):
            cap = 50
        cap = max(1, min(200, cap))

        triggered = False
        trigger_msg = ""
        self_deaths_new = stats["self_deaths"] > stats["last_self_deaths"]
        mate_deaths_new = stats["mate_deaths"] > stats["last_mate_deaths"]

        if cond == "自己死亡" and self_deaths_new:
            triggered = True
            trigger_msg = f"自己死亡({stats['self_deaths']})"
        elif cond == "队友死亡" and mate_deaths_new:
            triggered = True
            trigger_msg = f"队友死亡({stats['mate_deaths']})"
        elif cond == "任意死亡" and (self_deaths_new or mate_deaths_new):
            triggered = True
            trigger_msg = f"任意死亡(自{stats['self_deaths']}/队{stats['mate_deaths']})"
        elif cond == "血量<30%":
            hp_pct = stats.get("self_hp_percent", 100)
            if hp_pct < 30 and not stats.get("hp_low_triggered", False):
                triggered = True
                trigger_msg = f"血量<30%({hp_pct:.0f}%)"
                monitor.set_hp_low_triggered(True)
            elif hp_pct >= 30:
                monitor.set_hp_low_triggered(False)

        if not triggered:
            return
        monitor.update_last_deaths()

        # 先立即 fire：强度严格等于『下次触发强度』（_next_fire_strength 在三处已保证 ≤ cap）
        fire_val = self._next_fire_strength
        mgr = coyote_device.get_default_manager()
        ok = mgr.broadcast_fire(
            "all", strength=fire_val,
            time_ms=self.FIRE_DEFAULT_MS, override=True)

        self.debug_log(
            f"[Coyote] 事件触发({trigger_msg}) → 开火 强度={fire_val}（上限={cap}）"
            f" 时长={self.FIRE_DEFAULT_MS}ms 设备数={len(ok)}")
        if ok:
            self.debug_log(
                f"[Coyote] 提示: 若设备无输出，请在 DG-Lab APP 把通道强度上限拉到 >= {fire_val}")

        # 开火结束后，再更新『下次触发强度』（累加增量，上限 cap）
        self._next_fire_strength = max(
            self.FIRE_INITIAL, min(cap, self._next_fire_strength + increment))
        self.hub_lbl_next_fire.setText(f"下次强度: {self._next_fire_strength}")


    def setup_connections(self):
        """占位方法，防止其他代码调用时出错"""
        pass




class ConfigWindow(FluentWindow):
    signal_health_update = pyqtSignal(object, object, bool)
    signal_log = pyqtSignal(str)
    signal_error = pyqtSignal(str)
    signal_status = pyqtSignal(str)
    signal_srda_status = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        debug_log("ConfigWindow 初始化开始")
        self.ocr_worker = None
        self.capture_worker = None
        self.overlay_window = None
        self.current_state = None
        self.hotkey_timer = None

        # 连接跨线程信号
        self.signal_health_update.connect(self._on_health_update_signal)
        self.signal_log.connect(self._on_log_signal)
        self.signal_error.connect(self._on_error_signal)
        self.signal_status.connect(self._on_status_signal)

        try:
            self.init_window()
            debug_log("init_window 完成")
        except Exception as e:
            debug_log(f"init_window 失败: {e}")
            debug_log(traceback.format_exc())
            raise

        try:
            self.create_interfaces()
            debug_log("create_interfaces 完成")
        except Exception as e:
            debug_log(f"create_interfaces 失败: {e}")
            debug_log(traceback.format_exc())
            raise

        try:
            self.connect_signals()
            debug_log("connect_signals 完成")
        except Exception as e:
            debug_log(f"connect_signals 失败: {e}")
            debug_log(traceback.format_exc())
            raise

        try:
            self.init_overlay()
            debug_log("init_overlay 完成")
        except Exception as e:
            debug_log(f"init_overlay 失败: {e}")
            debug_log(traceback.format_exc())
            raise

        try:
            self.init_hotkey_timer()
            debug_log("init_hotkey_timer 完成")
        except Exception as e:
            debug_log(f"init_hotkey_timer 失败: {e}")
            debug_log(traceback.format_exc())
            raise

        try:
            self.init_shared_state()
            debug_log("init_shared_state 完成")
        except Exception as e:
            debug_log(f"init_shared_state 失败: {e}")
            debug_log(traceback.format_exc())

        debug_log("ConfigWindow 初始化完成")

    def init_shared_state(self):
        """启动共享内存 + 内置 GameValueMonitor，供 GameValueDetector 读取本应用进程的内存数据。"""
        import shared_state
        import game_value_monitor
        import dglab_client

        sw = shared_state.SharedStateWriter.get_instance()
        ok = sw.start()
        if ok:
            proc = sw.get_process_name()
            base = sw.base_address
            self.log(f"共享内存已启动: 进程={proc}, 地址=0x{base:X} (供 GameValueDetector 读取)")
            # 自动生成 JSON 配置到 DGLabGameController 的 Archive 目录
            self._auto_export_gvd_json()
        else:
            self.log("[警告] 共享内存启动失败，GameValueDetector 内存读取功能不可用")

        # 初始化内置监控器
        monitor = game_value_monitor.get_default_monitor()
        monitor.set_client(dglab_client.get_default_client())
        monitor.load_default_monitors()
        monitor.start()
        self.log(f"内置 GameValueMonitor 已启动 ({len(monitor.monitors)} 项监控规则)")

    def _auto_export_gvd_json(self):
        """共享内存启动后自动生成 JSON 写入 DGLabGameController Archive 目录。"""
        import json
        import os
        import shared_state
        try:
            sw = shared_state.SharedStateWriter.get_instance()
            proc_name = sw.get_process_name()
            module_name = sw.get_module_name()
            base_address = sw.get_base_address_for_json()

            def _monitor(field_key, scenarios, start_condition="Always"):
                offsets = sw.get_offsets_for_field(field_key)
                dtype = shared_state.FIELD_TYPES.get(field_key, "Int32")
                return {
                    "Module": module_name,
                    "BaseAddress": base_address,
                    "Offsets": offsets,
                    "Type": dtype,
                    "StartCondition": start_condition,
                    "Scenarios": scenarios,
                }

            cfg = {
                "Description": "星痕共鸣脉冲监控 - GameValueDetector 配置",
                "ProcessName": proc_name,
                "Is32Bit": False,
                "Monitors": [
                    _monitor("any_player_dead", [
                        # Increased 只在值"增加"时触发（差值>0），安全：避免 0>=0=true 的误触发
                        {"Scenario": "Increased", "CompareValue": 0,
                         "Action": "Fire", "ActionMode": "Default",
                         "ActionValue": 1, "Time": 3000, "Overrides": True},
                    ]),
                    _monitor("dead_count", [
                        # Increased：阵亡人数增加时触发 Fire
                        {"Scenario": "Increased", "CompareValue": 0,
                         "Action": "Fire", "ActionMode": "Default",
                         "ActionValue": 1, "Time": 2000, "Overrides": True},
                    ]),
                    _monitor("self_hp_percent", [
                        # 血量百分比减少时 → 强度增加
                        {"Scenario": "Decreased", "CompareValue": 0,
                         "Action": "SetStrengthAdd", "ActionMode": "ChangePercent",
                         "ActionValue": 1, "Time": 1000, "Overrides": False},
                        # 血量百分比增加时 → 强度减少（恢复回血）
                        {"Scenario": "Increased", "CompareValue": 0,
                         "Action": "SetStrengthSub", "ActionMode": "Percent",
                         "ActionValue": 0.2, "Time": 1000, "Overrides": False},
                    ]),  # 不设 ValueNotZero（有bug，默认全0时永远不满足，导致 dataValue 永远不更新）
                    _monitor("current_pulse", [
                        # 脉冲增加 → 强度微调增加
                        {"Scenario": "Increased", "CompareValue": 0,
                         "Action": "SetStrengthAdd", "ActionMode": "Default",
                         "ActionValue": 0.1, "Time": 1000, "Overrides": False},
                        # 脉冲减少 → 强度微调减少
                        {"Scenario": "Decreased", "CompareValue": 0,
                         "Action": "SetStrengthSub", "ActionMode": "Default",
                         "ActionValue": 0.1, "Time": 1000, "Overrides": False},
                    ]),
                ],
            }

            archive_dir = r"G:\DGLabGameController\Data\Modules\GameValueDetector\Archive"
            if os.path.isdir(archive_dir):
                out_path = os.path.join(archive_dir, "星痕共鸣_GameValueDetector配置.json")
                with open(out_path, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                self.log(f"[共享内存] JSON 配置已自动写入: {out_path}")
            else:
                debug_log(f"[共享内存] Archive 目录不存在: {archive_dir}")
        except Exception as e:
            debug_log(f"[共享内存] JSON 自动导出失败: {e}")

    def push_state_to_api(self):
        """把当前最新状态写入共享内存 + 向 DGLabGameController 发送惩罚指令。"""
        try:
            import shared_state
            import dglab_client
            import game_value_monitor

            sw = shared_state.SharedStateWriter.get_instance()

            # 默认合理值（不再依赖已移除的 EventInterface 页面）
            current_pulse = 50
            next_bonus = 10
            one_click_bonus = 30
            trigger_count = getattr(self, "_trigger_count_total", 0)

            # 如果未启动共享内存写入，仍通过 monitor 触发郊狼动作（game_value_monitor 内部实现了 Coyote 联动）
            self_health = getattr(self, "_last_self_health", None)
            players = getattr(self, "_last_players_list", None) or []

            hp_pct = 0.0
            cur_hp = 0
            max_hp = 0
            if self_health:
                hp_pct = float(getattr(self_health, 'health_percent', 0.0))
                cur_hp = int(getattr(self_health, 'current_hp', 0))
                max_hp = int(getattr(self_health, 'max_hp', 0))

            dead_count = sum(1 for p in players if getattr(p, 'is_dead', False) or getattr(p, 'current_hp', 1) <= 0)
            if self_health and (getattr(self_health, 'is_dead', False) or cur_hp <= 0):
                dead_count += 1
            any_dead = dead_count > 0
            player_count = len(players) + 1

            # 写入共享内存（仅在启动时）
            if sw.is_started:
                sw.update(
                    current_pulse=current_pulse,
                    next_bonus=next_bonus,
                    one_click_bonus=one_click_bonus,
                    trigger_count=trigger_count,
                    self_hp_percent=hp_pct,
                    self_current_hp=cur_hp,
                    self_max_hp=max_hp,
                    any_player_dead=any_dead,
                    dead_count=dead_count,
                    player_count=player_count,
                )

            # 内置 GameValueMonitor 评估场景 + 触发动作
            monitor = game_value_monitor.get_default_monitor()
            monitor.update_state(
                current_pulse=current_pulse,
                next_bonus=next_bonus,
                one_click_bonus=one_click_bonus,
                trigger_count=trigger_count,
                self_hp_percent=hp_pct,
                self_current_hp=cur_hp,
                self_max_hp=max_hp,
                any_player_dead=any_dead,
                dead_count=dead_count,
                player_count=player_count,
            )

        except Exception:
            pass

    def init_hotkey_timer(self):
        self.hotkey_timer = QTimer(self)
        self.hotkey_timer.timeout.connect(self.check_hotkey)
        self.hotkey_timer.start(50)

    def check_hotkey(self):
        if self.overlay_window:
            self.overlay_window.check_hotkey()

    def init_overlay(self):
        if self.control_interface.switch_overlay.isChecked():
            self.overlay_window = OverlayWindow()
            self.update_overlay_with_defaults()
            self.overlay_window.show()

    def update_overlay_with_defaults(self):
        if self.overlay_window:
            # 使用郊狼控制页面的触发条件组合框（不再依赖已移除的 EventInterface）
            condition_text = self.dglab_interface.hub_combo_cond.currentText() \
                if getattr(self, "dglab_interface", None) else "任意死亡"
            self.overlay_window.update_state(
                50,   # current_pulse 默认值
                10,   # next_bonus 默认值
                0,    # trigger_count
                30,   # one_click_bonus 默认值
                condition_text
            )

    def init_window(self):
        self.setWindowTitle("星痕共鸣 - 事件监控器")
        self.resize(560, 750)

        setTheme(Theme.DARK)

    def create_interfaces(self):
        self.control_interface = ControlInterface()
        self.health_interface = HealthInterface()
        self.dglab_interface = DGLabDebugInterface()

        self.addSubInterface(
            self.control_interface, FIF.HOME, "控制面板",
            position=NavigationItemPosition.TOP
        )
        self.addSubInterface(
            self.health_interface, FIF.HEART, "血量状态",
            position=NavigationItemPosition.TOP
        )
        self.addSubInterface(
            self.dglab_interface, FIF.PALETTE, "郊狼控制",
            position=NavigationItemPosition.TOP
        )

    def connect_signals(self):
        self.control_interface.btn_start_ocr.clicked.connect(self.toggle_ocr)
        self.control_interface.btn_scan_window.clicked.connect(self.scan_windows)
        self.control_interface.switch_overlay.checkedChanged.connect(self.toggle_overlay)
        # 抓包模式信号
        self.control_interface.radio_ocr.clicked.connect(lambda: self.on_mode_changed(False))
        self.control_interface.radio_capture.clicked.connect(lambda: self.on_mode_changed(True))
        self.control_interface.btn_start_capture.clicked.connect(self.toggle_capture)
        self.control_interface.btn_refresh_if.clicked.connect(self.refresh_interfaces)
        # 清空玩家记录
        self.health_interface.clear_players_requested.connect(self._on_clear_players_requested)
        # 设置自身名称
        self.health_interface.self_name_changed.connect(self._on_self_name_changed)
        # 默认显示抓包卡片
        self.control_interface.set_capture_mode(True)
        # SRDA 状态信号连接到悬浮窗
        self.signal_srda_status.connect(self._on_srda_status)

    def on_mode_changed(self, capture_mode: bool):
        """模式切换回调"""
        # 如果有正在运行的工作线程，先停止
        if self.ocr_worker and self.ocr_worker.isRunning():
            self.stop_ocr()
        if self.capture_worker and self.capture_worker.is_alive():
            self.stop_capture()
        self.control_interface.set_capture_mode(capture_mode)
        if capture_mode:
            self.log("已切换到抓包模式")
            if not is_admin():
                self.log("[警告] 抓包需要管理员权限，请以管理员身份重启程序")
        else:
            self.log("已切换到OCR模式")

    def refresh_interfaces(self):
        """刷新网卡列表"""
        self.control_interface.refresh_interfaces()
        self.log(f"已刷新网卡列表 ({self.control_interface.combo_interface.count()} 个)")

    def toggle_capture(self):
        """切换抓包启动/停止"""
        debug_log(f"toggle_capture 被调用，capture_worker={'None' if self.capture_worker is None else '存在'}")
        if self.capture_worker is not None:
            debug_log(f"capture_worker.is_alive() = {self.capture_worker.is_alive()}")

        if self.capture_worker is None or not self.capture_worker.is_alive():
            debug_log("判断为未运行，调用 start_capture")
            self.start_capture()
        else:
            debug_log("判断为运行中，调用 stop_capture")
            self.stop_capture()

    def start_capture(self):
        """启动抓包 - 使用 SRDC (Node.js SEA 打包版) 的 HTTP API"""
        import packet_capture as pc

        if not os.path.exists(pc.SRDC_SERVER_JS):
            QMessageBox.warning(self, "警告",
                f"未找到 SRDC 程序:\n{pc.SRDC_SERVER_JS}\n\n"
                "请确认 StarResonanceDamageCounter 已正确放置")
            return

        self.log("使用 StarResonanceDamageCounter HTTP API 获取战斗数据")
        self.log(f"程序: {pc.SRDC_SERVER_JS}")
        self.log(f"API 地址: {pc.SRDC_API_URL}")

        # 检查 SRDC 是否在运行
        try:
            import psutil
            srdc_running = False
            for proc in psutil.process_iter(['name', 'pid']):
                try:
                    name = proc.info['name'] or ''
                    if 'star-resonance-damage-counter' in name.lower():
                        srdc_running = True
                        self.log(f"检测到 SRDC 进程 (PID={proc.info['pid']})")
                        break
                except Exception:
                    pass
            if not srdc_running:
                self.log("未检测到 SRDC 进程，将自动启动...")
        except Exception:
            pass

        # 启动 SRDC API 读取器
        self.capture_worker = pc.SrdcApiReader(
            on_log=lambda msg: self.signal_log.emit(msg),
            on_error=lambda e: self.signal_error.emit(f"[读取错误] {e}"),
            on_status=lambda s: self.signal_status.emit(s),
            on_state=lambda p, all_p, has_t: self.signal_health_update.emit(p, all_p, has_t),
            on_srda_status=lambda s, d: self.signal_srda_status.emit(s, d),
            poll_interval=0.5
        )
        self.capture_worker.start()

        self.control_interface.btn_start_capture.setText("停止抓包")
        self.control_interface.btn_start_capture.setIcon(FIF.PAUSE)
        self.control_interface.combo_interface.setEnabled(False)
        self.control_interface.input_target_ip.setEnabled(False)
        self.health_interface.set_capture_status(True)
        self.log("SRDC API 读取器已启动，等待数据...请进入游戏战斗")

    def _on_health_update_signal(self, health_info, players=None, has_team=False):
        """主线程中的血量更新处理（由信号触发）"""
        self.on_capture_health_update(health_info, players, has_team)

    def _on_log_signal(self, msg: str):
        """主线程中的日志处理（由信号触发）"""
        self.log(msg)

    def _on_error_signal(self, msg: str):
        """主线程中的错误处理（由信号触发）"""
        self.log(msg)

    def _on_status_signal(self, status: str):
        """主线程中的状态处理（由信号触发）"""
        self.on_capture_status(status)

    def _on_srda_status(self, status: str, detail: str):
        """SRDA 状态更新（由信号触发）"""
        if self.overlay_window:
            self.overlay_window.update_srda_status(status, detail)

    def on_capture_health_update(self, health_info, players=None, has_team=False):
        """抓包血量更新回调"""
        debug_log(f"抓包血量更新: {health_info.name} HP={health_info.current_hp}/{health_info.max_hp} ({health_info.health_percent:.1f}%)")
        # 更新血量状态界面
        from ocr_engine import PlayerHealth
        ph = PlayerHealth(
            name=health_info.name,
            uid=getattr(health_info, 'uid', 0),
            profession=getattr(health_info, 'profession', ''),
            health_percent=health_info.health_percent,
            current_hp=health_info.current_hp,
            max_hp=health_info.max_hp,
            is_self=True
        )

        # 构造队伍玩家列表（排除自身）
        team_players = []
        if players:
            for p in players:
                if not p.is_self:
                    team_players.append(PlayerHealth(
                        name=p.name,
                        uid=getattr(p, 'uid', 0),
                        profession=getattr(p, 'profession', ''),
                        health_percent=p.health_percent,
                        current_hp=p.current_hp,
                        max_hp=p.max_hp,
                        is_self=False
                    ))

        # 缓存最新数据并推送到 HTTP API 共享内存
        self._last_self_health = ph
        self._last_players_list = team_players
        self._last_has_team = bool(has_team)
        self.push_state_to_api()

        self.health_interface.update_health(ph, team_players, has_team)

        # 更新悬浮窗
        if self.overlay_window:
            try:
                self.overlay_window.update_health(ph, team_players, has_team)
            except Exception:
                pass

    def stop_capture(self, force_stop_srdc: bool = False):
        """停止抓包

        Args:
            force_stop_srdc: 是否强制停止 SRDC 进程（用于清空缓存
        """
        debug_log(f"stop_capture 被调用, force_stop_srdc={force_stop_srdc}")
        try:
            if self.capture_worker:
                debug_log("调用 capture_worker.stop()")
                if hasattr(self.capture_worker, 'stop') and callable(getattr(self.capture_worker, 'stop')):
                    import inspect
                    sig = inspect.signature(self.capture_worker.stop)
                    if 'force_stop_srdc' in sig.parameters:
                        self.capture_worker.stop(force_stop_srdc=force_stop_srdc)
                    else:
                        self.capture_worker.stop()
                else:
                    self.capture_worker.stop()
                debug_log("等待 capture_worker 线程结束...")
                self.capture_worker.join(timeout=5)
                debug_log(f"capture_worker is_alive: {self.capture_worker.is_alive()}")
                self.capture_worker = None
        except Exception as e:
            debug_log(f"停止 capture_worker 异常: {e}")
            debug_log(traceback.format_exc())
            self.log(f"[警告] 停止抓包时发生异常: {e}")
            # 强制置空，避免状态不一致
            self.capture_worker = None

        # 无论如何都恢复按钮状态
        self.control_interface.btn_start_capture.setText("启动抓包")
        self.control_interface.btn_start_capture.setIcon(FIF.PLAY)
        self.control_interface.combo_interface.setEnabled(True)
        self.control_interface.input_target_ip.setEnabled(True)

        self.health_interface.set_capture_status(False)
        if self.overlay_window:
            self.overlay_window.update_srda_status("stopped")
        if force_stop_srdc:
            self.log("抓包已停止，SRDC 进程已终止，缓存已清空")
        else:
            self.log("抓包已停止")

    def _on_clear_players_requested(self):
        """清空玩家记录请求 - 重启SRDC以清空缓存"""
        debug_log("清空玩家记录，重启SRDC")
        # 清空悬浮窗
        if self.overlay_window:
            self.overlay_window.clear_players()
        # 如果抓包正在运行，重启抓包以清空SRDC缓存
        if self.capture_worker and self.capture_worker.is_alive():
            self.log("正在重启抓包以清空数据缓存...")
            # 使用定时器延迟重启，避免阻塞UI
            QTimer.singleShot(100, self._restart_capture_for_clear)
        else:
            self.log("玩家记录已清空")

    def _restart_capture_for_clear(self):
        """重启抓包（用于清空缓存）"""
        if self.control_interface.is_capture_mode():
            self.stop_capture(force_stop_srdc=True)
            # 等一下再启动，确保 SRDC 进程完全退出
            QTimer.singleShot(3000, self._start_capture_after_clear)

    def _start_capture_after_clear(self):
        """清空后重新启动抓包"""
        self.start_capture()
        self.log("抓包已重启，数据缓存已清空")

    def _on_self_name_changed(self, name: str):
        """自身名称改变回调"""
        debug_log(f"设置自身名称: {name}")
        self.log(f"设置自身玩家名称: {name}")
        if self.capture_worker and hasattr(self.capture_worker, 'set_self_name'):
            self.capture_worker.set_self_name(name)
        if self.overlay_window:
            self.overlay_window.set_self_name(name)

    def on_capture_status(self, status: str):
        """抓包状态回调"""
        debug_log(f"抓包状态: {status}")
        if status == "no_permission":
            QMessageBox.critical(self, "错误",
                "抓包需要管理员权限!\n请以管理员身份重启程序")
            self.control_interface.btn_start_capture.setText("启动抓包")
            self.control_interface.btn_start_capture.setIcon(FIF.PLAY)

    def scan_windows(self):
        self.log("正在扫描所有窗口...")
        keyword = self.control_interface.input_window_keyword.text().strip()
        monitor = WindowMonitor(keyword)
        monitor.print_all_windows()
        all_windows = monitor.list_all_windows()

        self.log(f"找到 {len(all_windows)} 个可见窗口:")
        for w in all_windows[:20]:
            self_marker = " [自身]" if w.get('is_self') else ""
            self.log(f"  - PID={w.get('pid','?')}{self_marker} [{w['process']}] {w['title']} ({w['width']}x{w['height']})")

        if len(all_windows) > 20:
            self.log(f"  ... 还有 {len(all_windows) - 20} 个窗口")

        matched = monitor.find_window()
        if matched:
            self.log(f"\n匹配到窗口: [{matched[2]}] {matched[1]}")
        else:
            self.log(f"\n未找到匹配关键词 '{keyword}' 的窗口")
            self.log("请查看控制台输出的窗口列表，修改关键词后重新扫描")

    def toggle_ocr(self):
        if self.ocr_worker is None or not self.ocr_worker.isRunning():
            self.start_ocr()
        else:
            self.stop_ocr()

    def start_ocr(self):
        keyword = self.control_interface.input_window_keyword.text().strip()
        if not keyword:
            QMessageBox.warning(self, "警告", "请输入窗口关键词")
            return

        debug_log(f"启动OCR，关键词: '{keyword}'")
        monitor = WindowMonitor(keyword)
        debug_log("启动前扫描所有窗口:")
        monitor.print_all_windows()

        matched = monitor.find_window()
        if not matched:
            QMessageBox.warning(self, "警告", 
                f"未找到匹配关键词 '{keyword}' 的游戏窗口\n"
                "请先启动游戏，或修改窗口关键词\n"
                "可点击「扫描窗口」查看所有可用窗口")
            return

        debug_log(f"找到游戏窗口: {matched[1]} (进程: {matched[2]})")
        self.log(f"正在启动截图器 (关键词: {keyword})...")
        self.ocr_worker = OCRWorker(keyword)
        self.ocr_worker.state_updated.connect(self.on_state_updated)
        self.ocr_worker.error_message.connect(self.on_ocr_error)
        self.ocr_worker.window_found.connect(self.on_window_found)
        self.ocr_worker.start()

        self.control_interface.btn_start_ocr.setText("停止截图器")
        self.control_interface.btn_start_ocr.setIcon(FIF.PAUSE)
        self.control_interface.input_window_keyword.setEnabled(False)
        self.control_interface.btn_scan_window.setEnabled(False)
        self.health_interface.set_ocr_status(True)

    def stop_ocr(self):
        if self.ocr_worker:
            self.ocr_worker.stop()
            self.ocr_worker = None

        self.control_interface.btn_start_ocr.setText("启动截图器")
        self.control_interface.btn_start_ocr.setIcon(FIF.PLAY)
        self.control_interface.input_window_keyword.setEnabled(True)
        self.control_interface.btn_scan_window.setEnabled(True)

        self.health_interface.set_ocr_status(False)
        self.log("截图器已停止")

    def on_state_updated(self, state):
        self.current_state = state

        # 缓存 OCR 模式最新数据并写入共享内存
        self._trigger_count_total = getattr(state, "trigger_count", 0)
        self._last_self_health = getattr(state, "self_health", None)
        self._last_players_list = getattr(state, "players", None) or []
        self._last_has_team = bool(getattr(state, "has_team_list", False))
        self.push_state_to_api()

        if self.overlay_window and self.control_interface.switch_overlay.isChecked():
            condition_text = self.dglab_interface.hub_combo_cond.currentText() \
                if getattr(self, "dglab_interface", None) else "任意死亡"
            self.overlay_window.update_state(
                state.current_pulse,
                state.next_bonus,
                state.trigger_count,
                state.one_click_bonus,
                condition_text
            )
            if state.self_health:
                self.overlay_window.update_health(
                    state.self_health.name,
                    state.self_health.health_percent,
                    state.self_health.current_hp,
                    state.self_health.max_hp
                )

        self.health_interface.update_health(
            state.self_health,
            state.players,
            state.has_team_list
        )

    def on_ocr_error(self, message: str):
        self.log(f"[OCR] {message}")

    def on_window_found(self, found: bool):
        if found:
            self.log("[OCR] 找到星痕共鸣客户端窗口")
        else:
            self.log("[OCR] 客户端窗口已关闭")

    def toggle_overlay(self, checked: bool):
        if checked:
            if not self.overlay_window:
                self.overlay_window = OverlayWindow()
            self.overlay_window.show()
        else:
            if self.overlay_window:
                self.overlay_window.hide()

    def log(self, message: str):
        self.control_interface.log(message)

    def closeEvent(self, event):
        if self.ocr_worker:
            self.ocr_worker.stop()
        if self.capture_worker:
            self.stop_capture()
        if self.overlay_window:
            self.overlay_window.close()
        try:
            import shared_state
            shared_state.SharedStateWriter.get_instance().stop()
        except Exception:
            pass
        event.accept()
