# -*- coding: utf-8 -*-
"""
豆包单题测试 -- 一个复杂题目，测送达 + 完成检测 + 提取。

用法:
    python test_doubao.py
    python test_doubao.py --timeout 300
"""

import os, sys, time, json, argparse
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import yaml

PROMPT = (
    "请写一篇不少于 600 字的深度回应。\n\n"
    "## 问题：翻译者的困境\n\n"
    "在一场多语言国际谈判中，翻译者不仅要传递字面意思，"
    "还要传递语气、潜台词和文化背景。但翻译者自己也有立场和情感。\n\n"
    "现在把这个问题搬到 AI 领域：\n"
    "在一个多 AI 协作系统中，负责汇总的那个 AI 本质上就是一个翻译者——"
    "它把所有人的发言翻译成一份汇总，而所有其他参与者只能通过这份汇总来了解彼此。\n\n"
    "请回答以下三个问题：\n\n"
    "1. 汇总者在压缩信息时，是否不可避免地会注入自己的理解偏差？"
    "这种偏差能被消除吗，还是只能被声明？\n\n"
    "2. 如果其他参与者对汇总有异议，应该有什么样的纠正机制？"
    "仅仅允许他们说「汇总不准确」够不够？\n\n"
    "3. 有没有可能完全不需要汇总者？"
    "比如让每个参与者直接阅读所有人的原文？"
    "如果这样做，又会带来什么新问题？\n\n"
    "请在最后给出你的结论：汇总这件事应该由谁来做、怎么做？"
)


def load_doubao_config():
    with open(os.path.join(SCRIPT_DIR, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    settings = cfg.get("settings", {})
    doubao_p = cfg["participants"].get("\u8c46\u5305", {})
    merged = {**settings, **doubao_p}
    return merged


def run_test(timeout: int = 300):
    from drivers.doubao import DouBaoDriver

    cfg = load_doubao_config()

    print("=" * 60)
    print(f"豆包单题测试   超时: {timeout}s")
    print(f"开始: {datetime.now().strftime('%H:%M:%S')}")
    print(f"题目长度: {len(PROMPT)} chars")
    print("=" * 60)

    driver = DouBaoDriver("\u8c46\u5305", cfg)
    driver.initialize()

    result = {
        "send_ok": False, "response_received": False,
        "response_chars": 0, "wait_seconds": 0, "error": None,
    }

    # -- send --
    driver.reset_send_flag()
    result["send_time"] = datetime.now().isoformat()
    try:
        ok = driver.send_message(PROMPT)
    except Exception as e:
        ok = False
        result["error"] = f"send: {e}"
    result["send_ok"] = ok

    if not ok:
        print(f"送达: FAIL - {result.get('error', 'unknown')}")
        return

    print("送达: OK")

    # -- wait --
    print(f"等待回应 (max {timeout}s)...", flush=True)
    t0 = time.time()
    try:
        driver.wait_for_response(timeout=timeout)
    except Exception as e:
        print(f"wait exception: {e}", flush=True)

    wait_s = round(time.time() - t0, 1)
    result["wait_seconds"] = wait_s
    print(f"等待完成: {wait_s}s")

    if not driver._is_page_alive():
        print("浏览器已关闭，中止。")
        return

    # -- extract --
    try:
        resp = driver.get_response()
    except Exception as e:
        resp = None
        result["error"] = f"extract: {e}"

    if resp and not driver._is_thinking_trace(resp):
        result["response_received"] = True
        result["response_chars"] = len(resp)
        print(f"提取: OK ({len(resp)} chars)")
        print(f"前 100 字:\n  {resp[:100]}...")
        print(f"后 100 字:\n  ...{resp[-100:]}")
    else:
        result["response_received"] = False
        print("提取: FAIL")

    # -- report --
    print(f"\n{'=' * 60}")
    tag = "PASS" if result["response_received"] else "FAIL"
    print(f"结果: [{tag}]  等待={wait_s}s  字数={result['response_chars']}")
    if result.get("error"):
        print(f"错误: {result['error']}")
    print("=" * 60)

    report_path = os.path.join(SCRIPT_DIR, "test_doubao_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now().isoformat(), **result},
                   f, ensure_ascii=False, indent=2)
    print(f"报告: {report_path}")
    print("浏览器保持打开。按回车退出脚本（浏览器会一起关闭），或直接手动关浏览器。")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    pa = argparse.ArgumentParser()
    pa.add_argument("--timeout", type=int, default=300)
    run_test(timeout=pa.parse_args().timeout)
