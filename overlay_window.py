from PyQt5.QtWidgets import QWidget, QLabel, QPushButton
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QPainter, QColor, QBrush, QPen
import win32api
import win32con
import win32gui
from datetime import datetime, timedelta
from collections import OrderedDict


PROFESSION_COLORS = {
    "森语者": QColor(144, 238, 144),
    "灵魂乐手": QColor(231, 76, 60),
    "冰魔导师": QColor(52, 152, 219),
    "巨刃守护者": QColor(210, 180, 140),
    "青岚骑士": QColor(26, 188, 156),
    "雷影剑士": QColor(155, 89, 182),
    "神射手": QColor(241, 196, 15),
    "神盾骑士": QColor(255, 215, 0),
    "赤炎狂战士": QColor(178, 34, 34),
}

DEFAULT_PROFESSION_COLOR = QColor(150, 150, 150)


PROFESSION_ALIASES = {
    "涤罪恶火·战斧": "赤炎狂战士",
}

def normalize_profession(profession: str) -> str:
    if not profession:
        return profession
    for alias, standard in PROFESSION_ALIASES.items():
        if alias in profession:
            return standard
    return profession

def get_profession_color(profession: str) -> QColor:
    if not profession:
        return DEFAULT_PROFESSION_COLOR
    norm = normalize_profession(profession)
    for key, color in PROFESSION_COLORS.items():
        if key in norm:
            return color
    return DEFAULT_PROFESSION_COLOR


class PlayerItemWidget(QWidget):
    """玩家信息卡片 - 名称在左，血量百分比居中，uid+职业在右"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.name = ""
        self.uid = 0
        self.profession = ""
        self.health_percent = 0.0
        self.is_self = False
        self.index = 0
        self.show_uid = True
        self.is_dead = False
        self.is_placeholder = False
        self.setFixedHeight(20)

    def set_data(self, name, uid, profession, health_percent, is_self, index, show_uid=True, is_placeholder=False):
        self.name = name
        self.uid = uid
        self.profession = profession
        self.health_percent = max(0.0, min(100.0, health_percent))
        self.is_self = is_self
        self.index = index
        self.show_uid = show_uid
        self.is_dead = (health_percent <= 0 and not is_placeholder)
        self.is_placeholder = is_placeholder
        self.update()

    def _draw_text_with_stroke(self, painter, x, y, width, height, flags, text, text_color, stroke_color=QColor(0, 0, 0, 180)):
        """绘制带描边的文字"""
        offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        for dx, dy in offsets:
            painter.setPen(stroke_color)
            painter.drawText(x + dx, y + dy, width, height, flags, text)
        painter.setPen(text_color)
        painter.drawText(x, y, width, height, flags, text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        w = self.width()
        h = self.height()

        bar_color = get_profession_color(self.profession)
        stroke_color = QColor(0, 0, 0, 200)
        text_color = QColor(255, 255, 255)
        sub_text_color = QColor(240, 240, 240)

        # 占位符模式：置灰显示
        if self.is_placeholder:
            text_color = QColor(150, 150, 150)
            sub_text_color = QColor(130, 130, 130)
            bar_color = QColor(100, 100, 100)
            stroke_color = QColor(0, 0, 0, 100)

        # 血条背景
        bg_color = QColor(50, 50, 50, 200)
        painter.setBrush(QBrush(bg_color))
        if self.is_dead:
            painter.setPen(QPen(QColor(231, 76, 60), 2))
        else:
            painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, w, h, 3, 3)

        # 血条前景（按职业颜色），死亡时不显示血条
        if not self.is_dead and not self.is_placeholder and self.health_percent > 0:
            fill_width = int(w * self.health_percent / 100)
            painter.setBrush(QBrush(bar_color))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(0, 0, fill_width, h, 3, 3)
        elif self.is_placeholder:
            # 占位符：显示灰色空血条背景
            painter.setBrush(QBrush(QColor(80, 80, 80, 150)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(0, 0, w, h, 3, 3)

        # 左侧：序号 + 玩家名称
        prefix = "★" if self.is_self else f"{self.index}."
        name_text = f"{prefix} {self.name}"

        if self.is_self:
            painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
        else:
            painter.setFont(QFont("Microsoft YaHei UI", 8))

        name_rect = painter.fontMetrics().boundingRect(name_text)
        max_name_width = int(w * 0.32)
        name_x = 5
        if name_rect.width() > max_name_width:
            elided = painter.fontMetrics().elidedText(name_text, Qt.ElideRight, max_name_width)
            self._draw_text_with_stroke(painter, name_x, 0, max_name_width, h,
                                        Qt.AlignLeft | Qt.AlignVCenter, elided,
                                        text_color, stroke_color)
        else:
            self._draw_text_with_stroke(painter, name_x, 0, name_rect.width() + 10, h,
                                        Qt.AlignLeft | Qt.AlignVCenter, name_text,
                                        text_color, stroke_color)

        # 中间：血量百分比 或 死亡状态
        painter.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
        if self.is_dead:
            hp_text = "死亡"
        elif self.is_placeholder:
            hp_text = "未检测"
        else:
            hp_text = f"{self.health_percent:.1f}%"
        hp_rect = painter.fontMetrics().boundingRect(hp_text)
        hp_x = (w - hp_rect.width()) // 2
        if self.is_dead:
            self._draw_text_with_stroke(painter, hp_x, 0, hp_rect.width(), h,
                                        Qt.AlignCenter | Qt.AlignVCenter, hp_text,
                                        QColor(231, 76, 60), stroke_color)
        else:
            self._draw_text_with_stroke(painter, hp_x, 0, hp_rect.width(), h,
                                        Qt.AlignCenter | Qt.AlignVCenter, hp_text,
                                        text_color, stroke_color)

        # 右侧：UID + 职业（靠右对齐），双排时不显示UID
        display_profession = normalize_profession(self.profession)
        right_text = ""
        if self.show_uid and self.uid and display_profession:
            right_text = f"{self.uid} {display_profession}"
        elif self.show_uid and self.uid:
            right_text = str(self.uid)
        elif display_profession:
            right_text = display_profession

        if right_text:
            painter.setFont(QFont("Microsoft YaHei UI", 7))
            right_rect = painter.fontMetrics().boundingRect(right_text)
            max_right_width = int(w * 0.35)
            if right_rect.width() > max_right_width:
                elided = painter.fontMetrics().elidedText(right_text, Qt.ElideLeft, max_right_width)
                self._draw_text_with_stroke(painter, w - max_right_width - 5, 0, max_right_width, h,
                                            Qt.AlignRight | Qt.AlignVCenter, elided,
                                            sub_text_color, stroke_color)
            else:
                self._draw_text_with_stroke(painter, w - right_rect.width() - 5, 0, right_rect.width(), h,
                                            Qt.AlignRight | Qt.AlignVCenter, right_text,
                                            sub_text_color, stroke_color)


class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.drag_position = None
        self.locked = False
        self._players = OrderedDict()
        self._max_players = 20
        self._timeout_seconds = 60
        self._self_name = ""

        # 尺寸常量 - 全部精确控制
        self._base_width = 320
        self._player_item_height = 20
        self._player_spacing = 3
        self._max_height = 500

        # 边距
        self._margin_left = 16
        self._margin_right = 16
        self._content_width = self._base_width - self._margin_left - self._margin_right

        # 各区域高度（固定部分）
        self._header_height = 36      # 标题栏
        self._divider_height = 1      # 分隔线
        self._section_title_height = 22  # "玩家血量"标题
        self._tip_height = 18         # 提示文字
        self._no_player_height = 28   # 无玩家提示
        self._info_line_height = 22   # 每行信息高度
        self._status_bar_height = 20  # 状态栏高度
        self._bottom_hint_height = 20 # 底部提示
        self._section_gap = 6         # 区块间距

        self._player_widgets = []
        self._current_player_rows = 0

        self.init_ui()
        self.installEventFilter(self)
        self._init_cleanup_timer()

    def _init_cleanup_timer(self):
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.timeout.connect(self._cleanup_expired_players)
        self._cleanup_timer.start(5000)

    def init_ui(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.setFixedWidth(self._base_width)
        self.setMinimumHeight(200)

        # 背景容器
        self.container = QWidget(self)
        self.container.setObjectName("container")
        self.container.setStyleSheet("""
            #container {
                background-color: rgba(32, 32, 32, 220);
                border-radius: 12px;
                border: 1px solid rgba(120, 180, 255, 180);
            }
        """)

        # ===== 绝对定位所有控件 =====
        y_cursor = 10  # 顶部内边距

        # --- 标题栏 ---
        self.title_label = QLabel("星痕共鸣 · 脉冲监控", self.container)
        title_font = QFont("Microsoft YaHei UI", 11, QFont.Bold)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet("color: #60A5FA;")
        self.title_label.move(self._margin_left, y_cursor)
        self.title_label.setFixedSize(160, 24)

        self.unlock_hint = QLabel("Ctrl+Alt+U", self.container)
        hint_font = QFont("Microsoft YaHei UI", 7)
        self.unlock_hint.setFont(hint_font)
        self.unlock_hint.setStyleSheet("color: rgba(255, 255, 255, 100);")
        self.unlock_hint.setVisible(False)
        self.unlock_hint.setFixedSize(60, 20)
        self.unlock_hint.move(self._base_width - self._margin_right - 24 - 60, y_cursor + 4)

        self.lock_btn = QPushButton("🔓", self.container)
        self.lock_btn.setFixedSize(24, 24)
        self.lock_btn.setCursor(Qt.PointingHandCursor)
        self.lock_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 30);
                border-radius: 4px;
            }
        """)
        self.lock_btn.setToolTip("点击锁定窗口（鼠标穿透）")
        self.lock_btn.clicked.connect(self.toggle_lock)
        self.lock_btn.move(self._base_width - self._margin_right - 24, y_cursor)

        y_cursor += self._header_height  # 标题栏高度

        # --- 分隔线1 ---
        self.divider1 = QWidget(self.container)
        self.divider1.setFixedHeight(self._divider_height)
        self.divider1.setStyleSheet("background-color: rgba(120, 180, 255, 100);")
        self.divider1.setGeometry(self._margin_left, y_cursor, self._content_width, self._divider_height)
        y_cursor += self._divider_height + self._section_gap

        # --- "玩家血量"标题 ---
        self.health_title = QLabel("玩家血量", self.container)
        health_title_font = QFont("Microsoft YaHei UI", 9, QFont.Bold)
        self.health_title.setFont(health_title_font)
        self.health_title.setStyleSheet("color: #34D399;")
        self.health_title.move(self._margin_left, y_cursor)
        self.health_title.setFixedSize(100, self._section_title_height)
        y_cursor += self._section_title_height

        # --- 提示文字 ---
        self.tip_label = QLabel("💡 若未检测到玩家则需切线", self.container)
        tip_font = QFont("Microsoft YaHei UI", 7)
        self.tip_label.setFont(tip_font)
        self.tip_label.setStyleSheet("color: #FF9800;")
        self.tip_label.move(self._margin_left, y_cursor)
        self.tip_label.setFixedSize(200, self._tip_height)
        y_cursor += self._tip_height + 2

        # --- 玩家血条区域的 y 起始位置记录 ---
        self._players_area_y = y_cursor

        # --- 玩家容器 ---
        self.players_container = QWidget(self.container)
        self.players_container.setGeometry(self._margin_left, y_cursor, self._content_width, self._no_player_height)

        # 无玩家提示
        self.no_player_label = QLabel("等待检测玩家...", self.players_container)
        no_player_font = QFont("Microsoft YaHei UI", 8)
        self.no_player_label.setFont(no_player_font)
        self.no_player_label.setStyleSheet("color: #9CA3AF;")
        self.no_player_label.setAlignment(Qt.AlignCenter)
        self.no_player_label.setGeometry(0, 0, self._content_width, self._no_player_height)
        self.no_player_label.show()

        y_cursor = self._players_area_y + self._no_player_height + self._section_gap

        # --- 分隔线2 ---
        self.divider2 = QWidget(self.container)
        self.divider2.setFixedHeight(self._divider_height)
        self.divider2.setStyleSheet("background-color: rgba(120, 180, 255, 50);")
        self.divider2_y = y_cursor
        self.divider2.setGeometry(self._margin_left, y_cursor, self._content_width, self._divider_height)
        y_cursor += self._divider_height + self._section_gap

        # --- 脉冲信息区域 ---
        self._info_start_y = y_cursor

        info_font = QFont("Microsoft YaHei UI", 10)

        self.current_pulse_label = QLabel("当前脉冲强度:  50", self.container)
        self.current_pulse_label.setFont(info_font)
        self.current_pulse_label.setStyleSheet("color: #FFFFFF;")
        self.current_pulse_label.move(self._margin_left, y_cursor)
        self.current_pulse_label.setFixedSize(self._content_width, self._info_line_height)
        y_cursor += self._info_line_height

        self.next_bonus_label = QLabel("下次触发加成:  +10", self.container)
        self.next_bonus_label.setFont(info_font)
        self.next_bonus_label.setStyleSheet("color: #FBBF24;")
        self.next_bonus_label.move(self._margin_left, y_cursor)
        self.next_bonus_label.setFixedSize(self._content_width, self._info_line_height)
        y_cursor += self._info_line_height

        self.trigger_count_label = QLabel("已触发次数:  0", self.container)
        self.trigger_count_label.setFont(info_font)
        self.trigger_count_label.setStyleSheet("color: #34D399;")
        self.trigger_count_label.move(self._margin_left, y_cursor)
        self.trigger_count_label.setFixedSize(self._content_width, self._info_line_height)
        y_cursor += self._info_line_height

        self.one_click_bonus_label = QLabel("一键点火加成:  +30", self.container)
        self.one_click_bonus_label.setFont(info_font)
        self.one_click_bonus_label.setStyleSheet("color: #F87171;")
        self.one_click_bonus_label.move(self._margin_left, y_cursor)
        self.one_click_bonus_label.setFixedSize(self._content_width, self._info_line_height)
        y_cursor += self._info_line_height

        self.condition_label = QLabel("加成条件:  自己受到伤害", self.container)
        self.condition_label.setFont(info_font)
        self.condition_label.setStyleSheet("color: #9CA3AF;")
        self.condition_label.move(self._margin_left, y_cursor)
        self.condition_label.setFixedSize(self._content_width, self._info_line_height)
        y_cursor += self._info_line_height

        y_cursor += self._section_gap

        # --- SRDC 状态栏 ---
        self.srda_status_label = QLabel("SRDC: 未启动", self.container)
        srda_font = QFont("Microsoft YaHei UI", 8)
        self.srda_status_label.setFont(srda_font)
        self.srda_status_label.setStyleSheet("color: #9CA3AF;")
        self.srda_status_label.move(self._margin_left, y_cursor)
        self.srda_status_label.setFixedSize(self._content_width, self._status_bar_height)
        self._srda_status_y = y_cursor
        y_cursor += self._status_bar_height

        # --- 底部提示 ---
        self.hint_label = QLabel("拖动可移动窗口", self.container)
        self.hint_label.setAlignment(Qt.AlignCenter)
        hint_font = QFont("Microsoft YaHei UI", 8)
        self.hint_label.setFont(hint_font)
        self.hint_label.setStyleSheet("color: rgba(255, 255, 255, 80);")
        self.hint_label.setGeometry(self._margin_left, y_cursor, self._content_width, self._bottom_hint_height)
        y_cursor += self._bottom_hint_height

        y_cursor += 10  # 底部内边距

        # 初始高度（无玩家状态）
        self._initial_height = y_cursor
        self.setFixedHeight(self._initial_height)
        self.container.setGeometry(0, 0, self._base_width, self._initial_height)

    def toggle_lock(self):
        if not self.locked:
            self.lock_window()
        else:
            self.unlock_window()

    def lock_window(self):
        self.locked = True
        self.lock_btn.setText("🔒")
        self.lock_btn.setToolTip("已锁定（Ctrl+Alt+U 解锁）")
        self.unlock_hint.setVisible(True)
        self._set_click_through(True)

    def unlock_window(self):
        self.locked = False
        self.lock_btn.setText("🔓")
        self.lock_btn.setToolTip("点击锁定窗口（鼠标穿透）")
        self.unlock_hint.setVisible(False)
        self._set_click_through(False)

    def _set_click_through(self, enable: bool):
        hwnd = int(self.winId())
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if enable:
            ex_style |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
        else:
            ex_style &= ~(win32con.WS_EX_TRANSPARENT)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

    def check_hotkey(self):
        if not self.locked:
            return
        ctrl = win32api.GetAsyncKeyState(win32con.VK_CONTROL) & 0x8000
        alt = win32api.GetAsyncKeyState(win32con.VK_MENU) & 0x8000
        u_key = win32api.GetAsyncKeyState(ord('U')) & 0x8000
        if ctrl and alt and u_key:
            self.unlock_window()

    def update_state(self, current_pulse: int, next_bonus: int,
                     trigger_count: int, one_click_bonus: int,
                     bonus_condition: str):
        self.current_pulse_label.setText(f"当前脉冲强度:  {current_pulse}")
        self.next_bonus_label.setText(f"下次触发加成:  +{next_bonus}")
        self.trigger_count_label.setText(f"已触发次数:  {trigger_count}")
        self.one_click_bonus_label.setText(f"一键点火加成:  +{one_click_bonus}")
        self.condition_label.setText(f"加成条件:  {bonus_condition if bonus_condition else '--'}")

    def update_srda_status(self, status: str, detail: str = ""):
        """更新 SRDC 状态栏

        Args:
            status: 状态类型 - "running" / "stopped" / "error" / "waiting"
            detail: 附加信息（如 PID、网卡、最后更新时间等）
        """
        text = "SRDC: "
        if status == "running":
            text += f"运行中 ({detail})" if detail else "运行中"
            self.srda_status_label.setStyleSheet("color: #34D399;")
        elif status == "stopped":
            text += "未启动"
            self.srda_status_label.setStyleSheet("color: #9CA3AF;")
        elif status == "error":
            text += f"异常 ({detail})" if detail else "异常"
            self.srda_status_label.setStyleSheet("color: #EF4444;")
        elif status == "waiting":
            text += f"等待数据 ({detail})" if detail else "等待数据"
            self.srda_status_label.setStyleSheet("color: #FBBF24;")
        else:
            text += detail or status
            self.srda_status_label.setStyleSheet("color: #9CA3AF;")
        self.srda_status_label.setText(text)

    def update_health(self, self_health=None, players=None, has_team=False,
                      player_name: str = None, health_percent: float = 0.0,
                      current_hp: int = 0, max_hp: int = 0):
        now = datetime.now()

        all_players = []
        if players:
            all_players = list(players)
        if self_health is not None:
            self_name = getattr(self_health, 'name', '')
            found = False
            for p in all_players:
                if getattr(p, 'name', '') == self_name and self_name:
                    found = True
                    break
            if not found and self_name:
                all_players.insert(0, self_health)

        if not all_players and player_name:
            class TempPlayer:
                pass
            tp = TempPlayer()
            tp.name = player_name
            tp.current_hp = current_hp
            tp.max_hp = max_hp
            tp.health_percent = health_percent
            tp.is_self = True
            tp.profession = ''
            tp.uid = 0
            all_players.append(tp)

        if players is not None:
            # 全量数据模式（抓包模式）：用新列表完全替换旧列表
            new_players = OrderedDict()
            for p in all_players:
                uid = getattr(p, 'uid', 0) or hash(getattr(p, 'name', ''))
                new_players[uid] = {
                    'data': p,
                    'last_update': now
                }
            self._players = new_players
        else:
            # 增量模式（OCR模式）：累积更新
            for p in all_players:
                uid = getattr(p, 'uid', 0) or hash(getattr(p, 'name', ''))
                self._players[uid] = {
                    'data': p,
                    'last_update': now
                }
                self._players.move_to_end(uid)

            while len(self._players) > self._max_players:
                self._players.popitem(last=False)

        self._refresh_player_display()

    def set_self_name(self, name: str):
        """设置自身玩家名称"""
        self._self_name = name
        self._refresh_player_display()

    def clear_players(self):
        """清空所有玩家记录"""
        self._players.clear()
        self._refresh_player_display()

    def _cleanup_expired_players(self):
        now = datetime.now()
        expired = []
        for uid, info in self._players.items():
            if now - info['last_update'] > timedelta(seconds=self._timeout_seconds):
                expired.append(uid)
        for uid in expired:
            del self._players[uid]
        if expired:
            self._refresh_player_display()

    def _clear_player_widgets(self):
        for w in self._player_widgets:
            w.setParent(None)
            w.deleteLater()
        self._player_widgets = []

    def _refresh_player_display(self):
        self._clear_player_widgets()

        player_list = list(self._players.values())

        # 如果设置了自身名称，检查是否在列表中
        self_player_found = False
        if self._self_name:
            for info in player_list:
                p = info['data']
                if getattr(p, 'name', '') == self._self_name:
                    self_player_found = True
                    break

        # 显示列表：自身玩家固定在第一位
        display_list = []
        if self._self_name and not self_player_found:
            # 未检测到自身，添加置灰占位符在第一位
            class PlaceholderPlayer:
                pass
            pp = PlaceholderPlayer()
            pp.name = self._self_name
            pp.uid = 0
            pp.profession = ''
            pp.health_percent = 0
            pp.is_self = True
            display_list.append({'data': pp, 'is_placeholder': True})

        # 自身玩家放在第一位，其他按顺序
        self_players = []
        other_players = []
        for info in player_list:
            p = info['data']
            if getattr(p, 'is_self', False):
                self_players.append(info)
            else:
                other_players.append(info)

        for info in self_players:
            display_list.append(info)
        for info in other_players:
            display_list.append(info)

        if not display_list:
            self.no_player_label.setVisible(True)
            self.players_container.setGeometry(self._margin_left, self._players_area_y, self._content_width, self._no_player_height)
            self.no_player_label.setGeometry(0, 0, self._content_width, self._no_player_height)
            self._relayout_below_players(self._no_player_height)
            return

        self.no_player_label.setVisible(False)

        total = len(display_list)
        if total <= 10:
            cols = 1
        else:
            cols = 2
        rows_per_col = (total + cols - 1) // cols

        if cols == 1:
            item_width = self._content_width
        else:
            item_width = (self._content_width - 8) // 2

        players_height = rows_per_col * self._player_item_height + (rows_per_col - 1) * self._player_spacing
        self.players_container.setGeometry(self._margin_left, self._players_area_y, self._content_width, players_height)

        for idx, info in enumerate(display_list):
            p = info['data']
            name = getattr(p, 'name', f'玩家{idx+1}')
            uid = getattr(p, 'uid', 0)
            pct = getattr(p, 'health_percent', 0.0)
            profession = getattr(p, 'profession', '')
            is_self = getattr(p, 'is_self', False)
            is_placeholder = info.get('is_placeholder', False)

            item = PlayerItemWidget(self.players_container)
            item.setFixedSize(item_width, self._player_item_height)
            item.set_data(name, uid, profession, pct, is_self, idx + 1, show_uid=(cols == 1), is_placeholder=is_placeholder)

            col_idx = idx // rows_per_col
            row_idx = idx % rows_per_col

            x = col_idx * (item_width + 8)
            y = row_idx * (self._player_item_height + self._player_spacing)
            item.move(x, y)
            item.show()

            self._player_widgets.append(item)

        self._relayout_below_players(players_height)

    def _relayout_below_players(self, players_height):
        """重新布局玩家区域以下的所有元素"""
        # 分隔线2 的新位置
        divider2_new_y = self._players_area_y + players_height + self._section_gap
        self.divider2.setGeometry(self._margin_left, divider2_new_y, self._content_width, self._divider_height)

        # 脉冲信息区域的新位置
        info_start_new_y = divider2_new_y + self._divider_height + self._section_gap
        dy = info_start_new_y - self._info_start_y

        self.current_pulse_label.move(self._margin_left, self._info_start_y + dy)
        self.next_bonus_label.move(self._margin_left, self._info_start_y + dy + self._info_line_height)
        self.trigger_count_label.move(self._margin_left, self._info_start_y + dy + self._info_line_height * 2)
        self.one_click_bonus_label.move(self._margin_left, self._info_start_y + dy + self._info_line_height * 3)
        self.condition_label.move(self._margin_left, self._info_start_y + dy + self._info_line_height * 4)

        # SRDA 状态栏
        srda_status_new_y = self._info_start_y + dy + self._info_line_height * 5 + self._section_gap
        self.srda_status_label.move(self._margin_left, srda_status_new_y)

        # 底部提示
        hint_new_y = srda_status_new_y + self._status_bar_height
        self.hint_label.setGeometry(self._margin_left, hint_new_y, self._content_width, self._bottom_hint_height)

        # 窗口总高度
        total_height = hint_new_y + self._bottom_hint_height + 10
        total_height = max(total_height, self._initial_height)
        total_height = min(total_height, self._max_height)

        self.setFixedHeight(total_height)
        self.container.setGeometry(0, 0, self._base_width, total_height)

    def mousePressEvent(self, event):
        if self.locked:
            return
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.locked:
            return
        if event.buttons() == Qt.LeftButton and self.drag_position:
            self.move(event.globalPos() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_position = None

    def eventFilter(self, obj, event):
        if event.type() == event.WindowActivate:
            pass
        return super().eventFilter(obj, event)
