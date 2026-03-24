"""Playwright driver for doubao.com web chat."""

import time
import os
from playwright.sync_api import sync_playwright, Page, BrowserContext
from .base import BaseDriver, DriverMode


SELECTORS = {
    "textarea": '[data-testid="chat_input_input"], textarea.semi-input-textarea, textarea',
    "send_button": 'button[data-testid="send_button"], button[class*="send"]',
    "stop_button": 'button[class*="stop"], button[aria-label*="停止"]',
    "receive_message": '[data-testid="receive_message"]',
    "message_text": '[data-testid="message_text_content"]',
    "markdown_body": '[class*="flow-markdown-body"]',
    "loading": '[class*="loading"], [class*="generating"], [class*="typing"]',
    "captcha": 'iframe[src*="captcha"], [class*="captcha"], [class*="verify"], [class*="Captcha"], [class*="Verify"]',
}


class DouBaoDriver(BaseDriver):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.url = config.get("url", "https://www.doubao.com/chat/")
        self._pw = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._message_count_before = 0

    def initialize(self):
        self._pw = sync_playwright().start()
        profile_dir = os.path.join(os.path.dirname(__file__), "..", "doubao_profile")

        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(self.url, wait_until="domcontentloaded")
        self._page.wait_for_timeout(3000)
        self._check_captcha()
        print(f"  [{self.name}] 豆包页面已加载: {self.url}")

    def _enable_thinking_mode(self):
        """Switch to 思考 mode via the mode dropdown at bottom toolbar."""
        try:
            btn = self._page.locator('[data-testid="deep-thinking-action-button"]').first
            if not btn.is_visible(timeout=1000):
                print(f"  [{self.name}] 未找到模式按钮")
                return False

            current = btn.inner_text().strip()
            if "思考" in current:
                return True

            btn.click()
            self._page.wait_for_timeout(800)

            # Find "思考" in the dropdown — it's a visible element near the bottom
            # with class containing "flex items-center gap-2" inside the popup
            options = self._page.locator("text=思考").all()
            for opt in options:
                try:
                    if not opt.is_visible(timeout=300):
                        continue
                    bbox = opt.bounding_box()
                    if not bbox or bbox["y"] < 0:
                        continue
                    cls = opt.evaluate("e => e.className") or ""
                    if "items-center" in cls or "gap-2" in cls:
                        opt.click()
                        self._page.wait_for_timeout(500)
                        new_text = btn.inner_text().strip()
                        if "思考" in new_text:
                            print(f"  [{self.name}] 已切换到思考模式")
                            return True
                except Exception:
                    continue

            # Fallback: click any visible 思考 in the dropdown area
            for opt in options:
                try:
                    bbox = opt.bounding_box()
                    if bbox and bbox["y"] > 300:
                        opt.click()
                        self._page.wait_for_timeout(500)
                        print(f"  [{self.name}] 已切换到思考模式")
                        return True
                except Exception:
                    continue

            self._page.keyboard.press("Escape")
            print(f"  [{self.name}] 未能切换思考模式")
            return False
        except Exception as e:
            print(f"  [{self.name}] 思考模式切换失败: {e}")
            return False

    def _check_captcha(self):
        """Detect captcha/verification and pause until user resolves it."""
        for selector in SELECTORS["captcha"].split(", "):
            selector = selector.strip()
            try:
                if self._page.locator(selector).first.is_visible(timeout=500):
                    print(f"\n  ⚠ [{self.name}] 检测到验证码！请在浏览器中手动完成验证。")
                    print(f"  [{self.name}] 验证完成后会自动继续...\n")
                    while True:
                        self._page.wait_for_timeout(2000)
                        still_visible = False
                        for s in SELECTORS["captcha"].split(", "):
                            try:
                                if self._page.locator(s.strip()).first.is_visible(timeout=500):
                                    still_visible = True
                                    break
                            except Exception:
                                continue
                        if not still_visible:
                            print(f"  [{self.name}] 验证码已通过，继续...")
                            self._page.wait_for_timeout(1000)
                            return
            except Exception:
                continue

    def _find_input(self):
        """Find the chat input element."""
        page = self._page
        for selector in SELECTORS["textarea"].split(", "):
            selector = selector.strip()
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=1000):
                    return el
            except Exception:
                continue
        return None

    def _count_messages(self) -> int:
        """Count bot (received) messages on the page."""
        try:
            return self._page.locator(SELECTORS["receive_message"]).count()
        except Exception:
            return 0

    def _is_generating(self) -> bool:
        """Check if the AI is still generating a response."""
        for selector in SELECTORS["stop_button"].split(", "):
            selector = selector.strip()
            try:
                if self._page.locator(selector).first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        # Also check if the last message text is still changing
        return False

    def send_message(self, text: str) -> bool:
        if self.mode == DriverMode.MANUAL:
            self.manual_send(text)
            return True

        try:
            self._message_count_before = self._count_messages()
            input_el = self._find_input()
            if not input_el:
                print(f"  [{self.name}] 找不到输入框，降级")
                self.downgrade_mode()
                if self.mode == DriverMode.MANUAL:
                    self.manual_send(text)
                return False

            input_el.click()
            self._page.wait_for_timeout(300)

            self._enable_thinking_mode()

            # Re-focus input after mode switch (dropdown may steal focus)
            input_el = self._find_input()
            if input_el:
                input_el.click()
                self._page.wait_for_timeout(300)

            import pyperclip
            pyperclip.copy(text)
            self._page.keyboard.press("Control+a")
            self._page.keyboard.press("Control+v")
            self._page.wait_for_timeout(500)

            # Try pressing Enter or clicking send button
            sent = False
            for selector in SELECTORS["send_button"].split(", "):
                try:
                    btn = self._page.locator(selector).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        sent = True
                        break
                except Exception:
                    continue

            if not sent:
                self._page.keyboard.press("Enter")

            self._page.wait_for_timeout(1000)
            self._check_captcha()
            print(f"  [{self.name}] 消息已发送")
            return True

        except Exception as e:
            self._last_error = str(e)
            print(f"  [{self.name}] 发送失败: {e}")
            self.downgrade_mode()
            if self.mode == DriverMode.MANUAL:
                self.manual_send(text)
            return False

    def wait_for_response(self, timeout: int = 180) -> bool:
        if self.mode == DriverMode.MANUAL:
            return True

        try:
            self._page.wait_for_timeout(3000)
            last_text = ""
            stable_count = 0

            start = time.time()
            while time.time() - start < timeout:
                self._check_captcha()
                new_count = self._count_messages()
                if new_count > self._message_count_before:
                    # Message appeared, now wait for it to stabilize
                    recv = self._page.locator(SELECTORS["receive_message"]).last
                    try:
                        current_text = recv.inner_text()
                    except Exception:
                        current_text = ""

                    if current_text == last_text and current_text:
                        stable_count += 1
                        if stable_count >= 2:
                            print(f"  [{self.name}] 回应完成")
                            return True
                    else:
                        stable_count = 0
                        last_text = current_text

                self._page.wait_for_timeout(3000)

            print(f"  [{self.name}] 等待超时 ({timeout}s)")
            return True

        except Exception as e:
            self._last_error = str(e)
            print(f"  [{self.name}] 等待出错: {e}")
            return True

    def get_response(self) -> str | None:
        if self.mode in (DriverMode.MANUAL, DriverMode.SEND_ONLY):
            return self.manual_collect()

        try:
            # Strategy 1: use data-testid="receive_message" to find bot messages,
            # then extract text from the last one
            recv_msgs = self._page.locator(SELECTORS["receive_message"]).all()
            if recv_msgs:
                last_msg = recv_msgs[-1]
                # Try to get markdown body inside the message
                md = last_msg.locator('[class*="flow-markdown-body"]')
                if md.count() > 0:
                    text = md.first.inner_text()
                    if text and text.strip():
                        return text.strip()
                # Fallback: get message_text_content inside
                txt_el = last_msg.locator('[data-testid="message_text_content"]')
                if txt_el.count() > 0:
                    text = txt_el.first.inner_text()
                    if text and text.strip():
                        return text.strip()
                # Fallback: get all text from the message
                text = last_msg.inner_text()
                if text and text.strip():
                    return text.strip()

            # Strategy 2: find last markdown body on page
            md_els = self._page.locator(SELECTORS["markdown_body"]).all()
            if md_els:
                text = md_els[-1].inner_text()
                if text and text.strip():
                    return text.strip()

            # Strategy 3: use copy button
            copy_btn = self._page.locator('[data-testid="message_action_copy"]').last
            if copy_btn.is_visible(timeout=1000):
                copy_btn.click()
                self._page.wait_for_timeout(500)
                import pyperclip
                text = pyperclip.paste()
                if text and text.strip():
                    return text.strip()

            print(f"  [{self.name}] 无法自动提取回应，请手动复制")
            self.downgrade_mode()
            return self.manual_collect()

        except Exception as e:
            self._last_error = str(e)
            print(f"  [{self.name}] 提取回应失败: {e}")
            return self.manual_collect()

    def cleanup(self):
        try:
            if self._context:
                self._context.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
