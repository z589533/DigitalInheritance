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
    CHAT_HINT = "（请直接在对话中回答，不要生成文档或文章卡片。）"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.url = config.get("url", "https://www.doubao.com/chat/")
        self._pw = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._message_count_before = 0
        self._sent_this_round = False

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
        return False

    def _detect_article_mode(self) -> bool:
        """Detect if Doubao switched to article/document mode.
        Article mode is fine — copy button still works. Only used for logging.
        """
        try:
            switch_btn = self._page.get_by_text("改用对话直接回答")
            if switch_btn.first.is_visible(timeout=1000):
                return True
        except Exception:
            pass
        return False

    def _is_page_alive(self) -> bool:
        """Check if the browser page is still alive."""
        try:
            self._page.evaluate("1")
            return True
        except Exception:
            return False

    def send_message(self, text: str) -> bool:
        if self.mode == DriverMode.MANUAL:
            self.manual_send(text)
            return True

        if self._sent_this_round:
            print(f"  [{self.name}] 本轮已发送过，跳过重复发送", flush=True)
            return True

        if not self._is_page_alive():
            print(f"  [{self.name}] 浏览器已关闭，无法发送", flush=True)
            return False

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

            input_el = self._find_input()
            if input_el:
                input_el.click()
                self._page.wait_for_timeout(300)

            full_text = text + "\n\n" + self.CHAT_HINT
            import pyperclip
            pyperclip.copy(full_text)
            self._page.keyboard.press("Control+a")
            self._page.keyboard.press("Control+v")
            self._page.wait_for_timeout(500)

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
            self._sent_this_round = True
            print(f"  [{self.name}] 消息已发送")
            return True

        except Exception as e:
            self._last_error = str(e)
            print(f"  [{self.name}] 发送失败: {e}")
            self.downgrade_mode()
            if self.mode == DriverMode.MANUAL:
                self.manual_send(text)
            return False

    def reset_send_flag(self):
        """Call before each new round to allow sending again."""
        self._sent_this_round = False

    def _new_msg_has_action_buttons(self) -> bool:
        """Check if the NEWEST receive_message (by index) has action buttons.
        Only checks the message at the position AFTER _message_count_before,
        so it won't be fooled by old messages' buttons.
        """
        try:
            all_recv = self._page.locator(SELECTORS["receive_message"])
            total = all_recv.count()
            if total <= self._message_count_before:
                return False
            new_msg = all_recv.nth(total - 1)
            for sel in ['[data-testid="message_action_copy"]',
                        '[data-testid*="message_action"]']:
                try:
                    btn = new_msg.locator(sel).first
                    if btn.is_visible(timeout=300):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def wait_for_response(self, timeout: int = 180) -> bool:
        """Wait for Doubao to finish responding.
        Simple strategy: wait for text to stay unchanged for 15 seconds.
        """
        if self.mode == DriverMode.MANUAL:
            return True

        try:
            self._page.wait_for_timeout(5000)
            last_text = ""
            stable_count = 0
            STABLE_NEEDED = 5

            start = time.time()
            while time.time() - start < timeout:
                if not self._is_page_alive():
                    print(f"  [{self.name}] 浏览器已关闭", flush=True)
                    return False

                self._check_captcha()

                new_count = self._count_messages()
                if new_count > self._message_count_before:
                    recv = self._page.locator(SELECTORS["receive_message"]).last
                    try:
                        current_text = recv.inner_text()
                    except Exception:
                        current_text = ""

                    if current_text == last_text and current_text:
                        stable_count += 1
                        if stable_count >= STABLE_NEEDED:
                            elapsed = int(time.time() - start)
                            print(f"  [{self.name}] 回应完成（文本稳定 15s，共 {elapsed}s）")
                            return True
                    else:
                        stable_count = 0
                        last_text = current_text

                elapsed = int(time.time() - start)
                if elapsed % 30 == 0 and elapsed > 0:
                    print(f"  [{self.name}] 等待中... ({elapsed}s)", flush=True)

                self._page.wait_for_timeout(3000)

            print(f"  [{self.name}] 等待超时 ({timeout}s)")
            return True

        except Exception as e:
            self._last_error = str(e)
            print(f"  [{self.name}] 等待出错: {e}")
            return True

    _THINKING_TRACE_KEYWORDS = ["跳过", "规划", "策略", "用户要求", "我需要",
                                "分析", "回顾", "梳理", "总结一下"]

    def _is_thinking_trace(self, text: str) -> bool:
        """Detect if extracted text is a thinking trace rather than a real response."""
        if not text:
            return True
        stripped = text.strip()
        if len(stripped) < 20:
            return True
        if len(stripped) < 120:
            hits = sum(1 for kw in self._THINKING_TRACE_KEYWORDS if kw in stripped)
            if hits >= 1:
                return True
        first_line = stripped.split("\n")[0]
        if len(first_line) > 10 and all(
            kw not in first_line for kw in ["#", "**", "对", "关于", "回应", "我",
                                            "豆包", "看完", "这", "结合", "针对"]
        ):
            hits = sum(1 for kw in self._THINKING_TRACE_KEYWORDS if kw in first_line)
            if hits >= 1 and len(stripped) < 200:
                return True
        return False

    def _extract_text_from_msg(self, msg) -> str | None:
        """Extract response text from a message element, skipping thinking blocks.
        Does NOT modify DOM (no display:none) to avoid page jitter.
        """
        # Try markdown body first (most likely contains the real response)
        md = msg.locator('[class*="flow-markdown-body"]')
        if md.count() > 0:
            text = md.last.inner_text()
            if text and text.strip() and not self._is_thinking_trace(text.strip()):
                return text.strip()

        # Try message_text_content
        txt_el = msg.locator('[data-testid="message_text_content"]')
        if txt_el.count() > 0:
            text = txt_el.last.inner_text()
            if text and text.strip() and not self._is_thinking_trace(text.strip()):
                return text.strip()

        return None

    def _copy_button_extract(self) -> str | None:
        """Use the copy button on the LAST receive_message to extract via clipboard.
        Sets a sentinel in clipboard first to verify the copy actually worked.
        """
        import pyperclip
        sentinel = "__doubao_copy_sentinel__"
        try:
            all_recv = self._page.locator(SELECTORS["receive_message"])
            total = all_recv.count()
            if total == 0:
                return None
            last_msg = all_recv.nth(total - 1)

            last_msg.hover(timeout=2000)
            self._page.wait_for_timeout(800)

            copy_btn = last_msg.locator('[data-testid="message_action_copy"]').first
            if not copy_btn.is_visible(timeout=2000):
                copy_btn = last_msg.locator('[data-testid*="message_action"]').first

            if copy_btn.is_visible(timeout=1000):
                pyperclip.copy(sentinel)
                copy_btn.click(force=True)
                self._page.wait_for_timeout(800)
                text = pyperclip.paste()
                if text and text != sentinel and text.strip():
                    return text.strip()
                print(f"  [{self.name}] 复制按钮点击后剪贴板未更新", flush=True)
        except Exception as e:
            print(f"  [{self.name}] copy_button_extract 异常: {e}", flush=True)
        return None

    def get_response(self) -> str | None:
        if self.mode in (DriverMode.MANUAL, DriverMode.SEND_ONLY):
            return self.manual_collect()

        if not self._is_page_alive():
            print(f"  [{self.name}] 浏览器已关闭，无法提取", flush=True)
            return None

        for attempt in range(3):
            try:
                text = self._copy_button_extract()
                if text and not self._is_thinking_trace(text):
                    return text

                recv_msgs = self._page.locator(SELECTORS["receive_message"]).all()
                if recv_msgs:
                    last_msg = recv_msgs[-1]
                    text = self._extract_text_from_msg(last_msg)
                    if text:
                        return text

                md_els = self._page.locator(SELECTORS["markdown_body"]).all()
                if md_els:
                    text = md_els[-1].inner_text()
                    if text and text.strip() and not self._is_thinking_trace(text.strip()):
                        return text.strip()

                if attempt < 2:
                    wait_s = (attempt + 1) * 5
                    print(f"  [{self.name}] 提取未成功，等待 {wait_s}s 重试 ({attempt+1}/3)...",
                          flush=True)
                    self._page.wait_for_timeout(wait_s * 1000)

            except Exception as e:
                self._last_error = str(e)
                print(f"  [{self.name}] 提取回应失败: {e}")
                if not self._is_page_alive():
                    print(f"  [{self.name}] 浏览器已关闭", flush=True)
                    return None
                if attempt < 2:
                    self._page.wait_for_timeout(3000)

        print(f"  [{self.name}] 多次提取失败，标记为 extraction_error")
        return None

    def cleanup(self):
        try:
            if self._context:
                self._context.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
