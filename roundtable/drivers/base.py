"""Abstract base class for all roundtable drivers."""

from abc import ABC, abstractmethod
from enum import Enum


class DriverMode(Enum):
    AUTO = "auto"
    SEND_ONLY = "send_only"
    MANUAL = "manual"


class BaseDriver(ABC):
    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.mode = DriverMode(config.get("mode", "auto"))
        self._last_error: str | None = None

    @abstractmethod
    def initialize(self):
        """Set up the driver (open browser, find window, etc)."""

    @abstractmethod
    def send_message(self, text: str) -> bool:
        """Send a message. Returns True on success."""

    @abstractmethod
    def wait_for_response(self, timeout: int = 180) -> bool:
        """Wait until the AI finishes responding. Returns True if detected."""

    @abstractmethod
    def get_response(self) -> str | None:
        """Extract the AI's response text. Returns None on failure."""

    def cleanup(self):
        """Optional cleanup when shutting down."""

    def downgrade_mode(self):
        if self.mode == DriverMode.AUTO:
            self.mode = DriverMode.SEND_ONLY
            print(f"  [{self.name}] 自动接收失败，降级为 send_only 模式")
        elif self.mode == DriverMode.SEND_ONLY:
            self.mode = DriverMode.MANUAL
            print(f"  [{self.name}] 自动发送失败，降级为 manual 模式")

    def manual_collect(self) -> str | None:
        """Fallback: watch clipboard for user to copy response."""
        import pyperclip
        sentinel = "__roundtable_manual_collect__"
        pyperclip.copy(sentinel)
        print(f"\n  请手动复制【{self.name}】的回应到剪贴板...")
        import time
        timeout = 120
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(1.5)
            text = pyperclip.paste()
            if text != sentinel and text.strip():
                return text.strip()
        print(f"  等待超时，跳过")
        return None

    def manual_send(self, text: str):
        """Fallback: copy to clipboard for user to paste."""
        import pyperclip
        pyperclip.copy(text)
        print(f"\n  消息已复制到剪贴板。请手动粘贴到【{self.name}】的窗口并发送。")
