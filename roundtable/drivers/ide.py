"""IDE notification driver for Cursor/Lingma.

Only SENDS a short notification to the AI's chat panel.
Never tries to extract responses — responses come via git files.
"""

import time
import pyperclip
import pyautogui
from pywinauto import Desktop
from .base import BaseDriver, DriverMode

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3


class IdeDriver(BaseDriver):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.window_keyword = config["window_keyword"]
        self.chat_shortcut = config.get("chat_shortcut")
        self.click_input = config.get("click_input", False)
        self.input_x_pct = config.get("input_x_pct", 0.5)
        self.input_y_pct = config.get("input_y_pct", 0.89)
        self._window = None

    def initialize(self):
        self._window = self._find_window()
        if self._window:
            title = self._window.window_text()
            print(f"  [{self.name}] 找到窗口: {title}")
        else:
            print(f"  [{self.name}] 未找到包含 '{self.window_keyword}' 的窗口")
            self.mode = DriverMode.MANUAL

    def _find_window(self):
        try:
            desktop = Desktop(backend="uia")
            for win in desktop.windows():
                title = win.window_text()
                if self.window_keyword in title:
                    return win
        except Exception as e:
            self._last_error = str(e)
        return None

    def _activate_window(self) -> bool:
        try:
            if not self._window or not self._window.exists():
                self._window = self._find_window()
            if not self._window:
                return False
            self._window.set_focus()
            time.sleep(0.5)
            return True
        except Exception:
            self._window = self._find_window()
            if self._window:
                try:
                    self._window.set_focus()
                    time.sleep(0.5)
                    return True
                except Exception:
                    pass
            return False

    def notify(self, message: str) -> bool:
        """Send a short notification to the IDE's chat panel."""
        if self.mode == DriverMode.MANUAL:
            pyperclip.copy(message)
            print(f"  [{self.name}] 通知已复制到剪贴板，请手动粘贴")
            return True

        try:
            if not self._activate_window():
                print(f"  [{self.name}] 无法激活窗口")
                pyperclip.copy(message)
                print(f"  [{self.name}] 通知已复制到剪贴板，请手动粘贴")
                return False

            time.sleep(0.3)

            if self.click_input:
                rect = self._window.rectangle()
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                x = rect.left + int(w * self.input_x_pct)
                y = rect.top + int(h * self.input_y_pct)
                pyautogui.click(x, y)
                time.sleep(0.8)
            elif self.chat_shortcut:
                keys = self.chat_shortcut.lower().split("+")
                pyautogui.hotkey(*keys)
                time.sleep(1.5)

            pyperclip.copy(message)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)
            pyautogui.press("enter")
            time.sleep(0.5)

            print(f"  [{self.name}] 通知已发送")
            return True

        except Exception as e:
            self._last_error = str(e)
            print(f"  [{self.name}] 发送通知失败: {e}")
            pyperclip.copy(message)
            print(f"  [{self.name}] 通知已复制到剪贴板，请手动粘贴")
            return False

    # BaseDriver interface — these are no-ops for IDE participants
    def send_message(self, text: str) -> bool:
        return self.notify(text)

    def wait_for_response(self, timeout: int = 300) -> bool:
        return True

    def get_response(self) -> str | None:
        return None

    def cleanup(self):
        pass
