"""
One-time setup: open doubao.com with a persistent browser profile,
let user log in. The login persists across launches automatically.

Also detects page selectors for debugging.

Usage:
    py setup_doubao.py           # Open browser, log in, detect selectors
    py setup_doubao.py --detect  # Just detect selectors (already logged in)
"""

import os
import sys
import time
from playwright.sync_api import sync_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(SCRIPT_DIR, "doubao_profile")
URL = "https://www.doubao.com/chat/"


def detect_selectors(page):
    """Print detected page elements for debugging."""
    print("\n--- 页面元素检测 ---")
    candidates = {
        "textarea": "textarea",
        "contenteditable": '[contenteditable="true"]',
        "textbox role": 'div[role="textbox"]',
        "编辑器": '[class*="editor"]',
    }
    for label, sel in candidates.items():
        try:
            count = page.locator(sel).count()
            if count > 0:
                print(f"  {label} ({sel}): {count} 个")
        except Exception:
            pass

    for pattern in ["send", "stop", "assistant", "bot", "markdown", "message", "chat", "content"]:
        try:
            matches = page.locator(f'[class*="{pattern}"]').count()
            if matches > 0:
                print(f"  [class*=\"{pattern}\"]: {matches} 个匹配")
        except Exception:
            pass

    # Try to find the actual input
    print("\n--- 输入框检测 ---")
    for sel in ["textarea", '[contenteditable="true"]', 'div[role="textbox"]', '[class*="input"]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1000):
                tag = el.evaluate("e => e.tagName")
                cls = el.evaluate("e => e.className")
                print(f"  可见输入框: <{tag}> class=\"{cls[:80]}\"")
                break
        except Exception:
            continue

    print("\n--- 消息元素检测 ---")
    for sel in ['[class*="message"]', '[class*="bubble"]', '[class*="content"]', '[data-role]']:
        try:
            count = page.locator(sel).count()
            if count > 0:
                first = page.locator(sel).first
                cls = first.evaluate("e => e.className")
                role = first.get_attribute("data-role") or ""
                print(f"  {sel}: {count} 个, first class=\"{cls[:60]}\" data-role=\"{role}\"")
        except Exception:
            pass


def main():
    detect_only = "--detect" in sys.argv

    print("=" * 50)
    if detect_only:
        print("豆包页面元素检测")
    else:
        print("豆包登录设置")
        print("浏览器会记住登录状态，只需登录一次。")
    print("=" * 50)
    print()

    pw = sync_playwright().start()
    browser = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        viewport={"width": 1280, "height": 800},
    )

    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto(URL, wait_until="domcontentloaded")
    print(f"浏览器已打开: {URL}")

    if detect_only:
        page.wait_for_timeout(3000)
        detect_selectors(page)
        browser.close()
        pw.stop()
        return

    print("\n请在浏览器中登录豆包（如果还没登录的话）。")
    print("登录成功后，这个脚本会自动检测页面元素。")
    print("等待 30 秒让你完成登录...\n")

    for i in range(30, 0, -5):
        print(f"  {i}s...")
        time.sleep(5)

    detect_selectors(page)

    print("\n登录状态已自动保存到: doubao_profile/")
    print("以后每次启动都会自动登录。")
    print("\n关闭浏览器中...")

    browser.close()
    pw.stop()
    print("完成！")


if __name__ == "__main__":
    main()
