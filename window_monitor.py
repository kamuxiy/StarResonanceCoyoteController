import win32gui
import win32ui
import win32con
import win32process
from ctypes import windll
from PIL import Image
import psutil
import os
import sys
import ctypes


class WindowMonitor:
    def __init__(self, keyword="星痕"):
        self.keyword = keyword
        self.hwnd = None
        self.last_rect = None
        self._self_pid = os.getpid()
        self._use_printwindow = True
        print(f"[WindowMonitor] 初始化，自身PID: {self._self_pid}, 关键词: {keyword}", flush=True)

    def _get_window_pid(self, hwnd):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid
        except Exception:
            return -1

    def _get_window_process_name(self, hwnd):
        try:
            pid = self._get_window_pid(hwnd)
            if pid == self._self_pid:
                return None
            process = psutil.Process(pid)
            return process.name()
        except Exception:
            return ""

    def _is_self_window(self, hwnd):
        try:
            pid = self._get_window_pid(hwnd)
            is_self = (pid == self._self_pid)
            return is_self
        except Exception:
            return False

    def find_window(self):
        def callback(hwnd, windows):
            if not win32gui.IsWindowVisible(hwnd):
                return True

            pid = self._get_window_pid(hwnd)
            if pid == self._self_pid:
                return True

            title = win32gui.GetWindowText(hwnd)
            process_name = self._get_window_process_name(hwnd)

            if process_name is None:
                return True

            keyword_lower = self.keyword.lower()
            title_match = keyword_lower in title.lower()
            process_match = keyword_lower in process_name.lower()

            if title_match or process_match:
                rect = win32gui.GetClientRect(hwnd)
                width = rect[2] - rect[0]
                height = rect[3] - rect[1]
                if width > 100 and height > 100:
                    print(f"[WindowMonitor] 匹配窗口: hwnd={hwnd}, pid={pid}, title='{title}', process='{process_name}', size={width}x{height}", flush=True)
                    windows.append((hwnd, title, process_name))
            return True

        windows = []
        print(f"[WindowMonitor] 开始查找窗口，关键词: '{self.keyword}'", flush=True)
        win32gui.EnumWindows(callback, windows)

        if windows:
            self.hwnd = windows[0][0]
            print(f"[WindowMonitor] 选择窗口: title='{windows[0][1]}', process='{windows[0][2]}', hwnd={self.hwnd}", flush=True)
            return windows[0]
        else:
            print(f"[WindowMonitor] 未找到匹配的窗口", flush=True)
        return None

    def list_all_windows(self):
        def callback(hwnd, windows):
            if not win32gui.IsWindowVisible(hwnd):
                return True

            pid = self._get_window_pid(hwnd)
            is_self = (pid == self._self_pid)

            title = win32gui.GetWindowText(hwnd)
            process_name = ""
            class_name = ""
            try:
                class_name = win32gui.GetClassName(hwnd)
            except Exception:
                pass

            if not is_self:
                try:
                    process_name = psutil.Process(pid).name()
                except Exception:
                    process_name = ""

            rect = win32gui.GetClientRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]

            if title and width > 100 and height > 100:
                windows.append({
                    'hwnd': hwnd,
                    'title': title,
                    'process': process_name,
                    'pid': pid,
                    'class_name': class_name,
                    'is_self': is_self,
                    'width': width,
                    'height': height
                })
            return True

        windows = []
        win32gui.EnumWindows(callback, windows)
        return windows

    def print_all_windows(self):
        """打印所有可见窗口信息，用于调试"""
        windows = self.list_all_windows()
        print(f"\n{'='*80}")
        print(f"[WindowMonitor] 自身PID: {self._self_pid}")
        print(f"[WindowMonitor] 共找到 {len(windows)} 个可见窗口:")
        print(f"{'-'*80}")
        for i, w in enumerate(windows):
            self_marker = " [自身]" if w['is_self'] else ""
            match_marker = ""
            keyword_lower = self.keyword.lower()
            if not w['is_self']:
                if (keyword_lower in w['title'].lower() or 
                    keyword_lower in w['process'].lower()):
                    match_marker = " [匹配]"
            print(f"  {i+1}. PID={w['pid']}{self_marker}{match_marker}")
            print(f"     标题: {w['title']}")
            print(f"     进程: {w['process']}")
            print(f"     类名: {w['class_name']}")
            print(f"     尺寸: {w['width']}x{w['height']}")
        print(f"{'='*80}\n")
        return windows

    def get_window_rect(self):
        if not self.hwnd:
            return None
        try:
            rect = win32gui.GetWindowRect(self.hwnd)
            self.last_rect = rect
            return rect
        except Exception:
            return None

    def get_client_size(self):
        if not self.hwnd:
            return None
        try:
            rect = win32gui.GetClientRect(self.hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            return width, height
        except Exception:
            return None

    def capture_window(self):
        if not self.hwnd:
            if not self.find_window():
                return None

        if self._is_self_window(self.hwnd):
            print(f"[WindowMonitor] 警告: 当前hwnd={self.hwnd}是自身窗口，重新查找", flush=True)
            self.hwnd = None
            if not self.find_window():
                return None

        try:
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
            width = right - left
            height = bottom - top

            if width <= 0 or height <= 0:
                print(f"[WindowMonitor] 窗口尺寸无效: {width}x{height}", flush=True)
                return None

            if not hasattr(self, '_capture_method'):
                self._capture_method = 'printwindow'
                self._tested_methods = set()

            img = self._try_capture(width, height)

            if img is not None and self._is_image_valid(img):
                return img

            if img is None or not self._is_image_valid(img):
                self._tested_methods.add(self._capture_method)
                if 'printwindow' not in self._tested_methods:
                    self._capture_method = 'printwindow'
                elif 'printwindow_full' not in self._tested_methods:
                    self._capture_method = 'printwindow_full'
                elif 'bitblt_nofocus' not in self._tested_methods:
                    self._capture_method = 'bitblt_nofocus'
                else:
                    self._capture_method = 'bitblt'
                print(f"[WindowMonitor] 切换截图方式: {self._capture_method}", flush=True)
                return self._try_capture(width, height)

            return img

        except Exception as e:
            print(f"截图失败: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return None

    def _is_image_valid(self, img):
        import numpy as np
        arr = np.array(img)
        if arr.max() == 0:
            return False
        non_black = (arr.sum(axis=2) > 30).sum()
        ratio = non_black / (arr.shape[0] * arr.shape[1])
        return ratio > 0.05

    def _try_capture(self, width, height):
        left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)

        hwnd_dc = win32gui.GetWindowDC(self.hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()

        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)

        try:
            if self._capture_method == 'printwindow':
                result = windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 2)
            elif self._capture_method == 'printwindow_full':
                result = windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 0)
            elif self._capture_method in ['bitblt', 'bitblt_nofocus']:
                if self._capture_method == 'bitblt':
                    try:
                        win32gui.SetForegroundWindow(self.hwnd)
                        import time
                        time.sleep(0.03)
                    except Exception:
                        pass
                screen_dc = win32gui.GetDC(0)
                screen_mfc = win32ui.CreateDCFromHandle(screen_dc)
                save_dc.BitBlt((0, 0), (width, height), screen_mfc, (left, top), win32con.SRCCOPY)
                screen_mfc.DeleteDC()
                win32gui.ReleaseDC(0, screen_dc)
                result = True
            else:
                result = False

            if not result and self._capture_method in ['printwindow', 'printwindow_full']:
                return None

            bmpinfo = save_bitmap.GetInfo()
            bmpstr = save_bitmap.GetBitmapBits(True)

            img = Image.frombuffer(
                'RGB',
                (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                bmpstr, 'raw', 'BGRX', 0, 1
            )

            return img

        finally:
            mfc_dc.DeleteDC()
            save_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwnd_dc)
            win32gui.DeleteObject(save_bitmap.GetHandle())

    def capture_region(self, x_ratio_start, y_ratio_start, x_ratio_end, y_ratio_end):
        img = self.capture_window()
        if img is None:
            return None

        width, height = img.size
        x1 = int(width * x_ratio_start)
        y1 = int(height * y_ratio_start)
        x2 = int(width * x_ratio_end)
        y2 = int(height * y_ratio_end)

        x1 = max(0, min(x1, width))
        y1 = max(0, min(y1, height))
        x2 = max(0, min(x2, width))
        y2 = max(0, min(y2, height))

        if x2 <= x1 or y2 <= y1:
            return None

        return img.crop((x1, y1, x2, y2))

    def capture_bottom_bar(self):
        return self.capture_region(0.0, 0.85, 1.0, 1.0)

    def capture_self_health(self):
        return self.capture_region(0.35, 0.90, 0.65, 0.98)

    def capture_player_name(self):
        return self.capture_region(0.08, 0.94, 0.25, 0.99)

    def capture_team_list(self):
        return self.capture_region(0.85, 0.15, 0.99, 0.55)

    def is_window_valid(self):
        if not self.hwnd:
            return False
        try:
            return win32gui.IsWindow(self.hwnd) and win32gui.IsWindowVisible(self.hwnd)
        except Exception:
            return False
