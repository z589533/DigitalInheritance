"""
圆桌自动化 — 基于文件交换 + Playwright 的多AI协作系统。

每轮完整流程：
  1. 写 topic.md（含 git 操作提示）→ git push
  2. 默写自己的回应 → push
  3. 通知衡/问（Ctrl+L 发消息）
  4. Playwright 发给豆包（思考模式）→ 收回应 → 写 response_豆包.md → push
  5. 轮询 git pull 等衡/问的 response 文件
  6. 收齐后编 summary.md → push → 通知衡/问 pull → 发汇总给豆包
  7. 诚卓审阅 → 继续 / 补充 / 结束

用法:
    py main.py                    完整圆桌
    py main.py --round 2          从第2轮续接
    py main.py --no-push          本地调试，不推送
"""

import os
import sys
import time
import argparse
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from core.formatter import (
    get_round_dir, write_topic_file, write_response_file,
    read_topic_file, read_response_file, collect_all_responses,
    write_summary_file,
)
from core.git_ops import git_add_commit_push, git_pull


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    with open(os.path.join(SCRIPT_DIR, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Doubao (Playwright)
# ---------------------------------------------------------------------------

def init_doubao(config: dict, settings: dict):
    from drivers.doubao import DouBaoDriver
    merged = {**settings, **config}
    driver = DouBaoDriver("豆包", merged)
    driver.initialize()
    return driver


def send_to_doubao(driver, text: str) -> str | None:
    """Send arbitrary text to doubao, return her response."""
    print("  [豆包] 发送中...")
    ok = driver.send_message(text)
    if not ok:
        print("  [豆包] 发送失败")
        return None
    print("  [豆包] 等待回应...")
    driver.wait_for_response(timeout=180)
    resp = driver.get_response()
    if resp:
        print(f"  [豆包] 收到回应（{len(resp)}字）")
    else:
        print("  [豆包] 未获取到回应")
    return resp


# ---------------------------------------------------------------------------
# IDE notification (衡 / 问)
# ---------------------------------------------------------------------------

def init_ide_drivers(participants: dict) -> dict:
    """Create IdeDriver instances for all ide-type participants."""
    from drivers.ide import IdeDriver
    drivers = {}
    for name, cfg in participants.items():
        if cfg.get("driver") == "ide":
            d = IdeDriver(name, cfg)
            d.initialize()
            drivers[name] = d
    return drivers


def notify_ide_all(ide_drivers: dict, message: str):
    """Send a short notification to all IDE participants."""
    for name, driver in ide_drivers.items():
        driver.notify(message)


# ---------------------------------------------------------------------------
# Git poll
# ---------------------------------------------------------------------------

def poll_for_responses(repo_root: str, round_dir: str,
                       names: list[str], timeout: int, interval: int) -> dict[str, str]:
    """Poll git for response files from file-based participants."""
    print(f"\n等待回应文件（每 {interval}s 拉取，{timeout}s 超时）...")
    print(f"待收: {', '.join(names)}")

    start = time.time()
    collected = {}

    while time.time() - start < timeout:
        git_pull(repo_root)
        for name in names:
            if name not in collected:
                resp = read_response_file(round_dir, name)
                if resp:
                    collected[name] = resp
                    print(f"  [{name}] 回应已到（{len(resp)}字）")

        missing = [n for n in names if n not in collected]
        if not missing:
            print("所有回应已收齐！")
            return collected

        elapsed = int(time.time() - start)
        print(f"  [{elapsed}s] 已收: {list(collected.keys())} | 待: {missing}")
        time.sleep(interval)

    missing = [n for n in names if n not in collected]
    if missing:
        print(f"\n超时，未收到: {missing}（跳过）")
    return collected


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def show_summary(responses: dict[str, str]):
    print("\n" + "=" * 60)
    print("本轮回应汇总")
    print("=" * 60)
    for name, text in responses.items():
        preview = text[:120].replace("\n", " ")
        print(f"\n  {name}（{len(text)}字）: {preview}...")
    print("\n" + "=" * 60)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="圆桌自动化")
    parser.add_argument("--round", type=int, default=1, help="起始轮次")
    parser.add_argument("--no-push", action="store_true", help="不推送 git")
    args = parser.parse_args()

    config = load_config()
    settings = config.get("settings", {})
    participants = config["participants"]
    repo_root = settings["repo_root"]
    rounds_dir = settings["rounds_dir"]
    do_push = not args.no_push

    all_names = list(participants.keys())
    git_poll_names = [n for n, p in participants.items()
                      if p["driver"] in ("git_file", "ide") and n != "默"]
    has_doubao = any(p["driver"] == "doubao" for p in participants.values())

    print("=" * 60)
    print("圆桌自动化")
    print("=" * 60)
    print(f"参与者: {', '.join(all_names)}")
    print(f"  需轮询回应: {', '.join(git_poll_names)}")
    print(f"  Playwright: {'豆包' if has_doubao else '无'}")
    print(f"仓库: {repo_root}")
    print()

    # --- Init drivers ---
    doubao_driver = None
    if has_doubao:
        print("--- 初始化豆包 Playwright ---")
        try:
            doubao_driver = init_doubao(participants.get("豆包", {}), settings)
        except Exception as e:
            print(f"  豆包初始化失败: {e}")
            has_doubao = False

    print("--- 初始化 IDE 通知 ---")
    ide_drivers = init_ide_drivers(participants)

    # --- State ---
    round_num = args.round
    topic = ""
    previous_responses = None

    if round_num > 1:
        prev_dir = get_round_dir(repo_root, rounds_dir, round_num - 1)
        previous_responses = collect_all_responses(prev_dir, all_names)
        if previous_responses:
            print(f"已加载第{round_num - 1}轮 {len(previous_responses)} 条回应")

    # === MAIN LOOP ===
    try:
        while True:
            print(f"\n{'=' * 60}")
            print(f"第{round_num}轮")
            print(f"{'=' * 60}")

            # --- 0. Get topic from moderator ---
            if round_num == args.round:
                topic = input("\n请输入本次讨论的议题：\n> ").strip()
                if not topic:
                    print("议题为空，退出。")
                    return
            else:
                choice = input(
                    "\n继续下一轮？"
                    "\n  直接回车 = 继续讨论"
                    "\n  输入内容 = 带补充继续"
                    "\n  q = 结束本议题"
                    "\n> "
                ).strip()
                if choice.lower() == "q":
                    break
                if choice:
                    topic = topic + f"\n\n【主持人补充】{choice}"

            round_dir = get_round_dir(repo_root, rounds_dir, round_num)

            # --- 1. Write topic.md ---
            print("\n--- 写入议题 ---")
            topic_path = write_topic_file(round_dir, round_num, topic, previous_responses)
            print(f"  {topic_path}")

            # --- 2. 默 writes own response (placeholder for this script) ---
            # 默 (me) will write response_默.md separately via the Cursor chat.
            # The script does NOT auto-generate 默's response.

            # --- 3. Git push topic ---
            if do_push:
                print("\n--- Git push 议题 ---")
                rel = os.path.relpath(topic_path, repo_root)
                git_add_commit_push(repo_root, [rel], f"圆桌第{round_num}轮议题")

            # --- 4. Notify 衡 / 问 ---
            if ide_drivers:
                print("\n--- 通知衡/问 ---")
                notify_msg = (
                    f"圆桌第{round_num}轮议题已推送。"
                    f"请 git pull 后查看 {rounds_dir}/round_{round_num:03d}/topic.md，"
                    f"写完回应后 git add + commit + push。"
                )
                notify_ide_all(ide_drivers, notify_msg)

            # --- 5. Send to 豆包 via Playwright ---
            doubao_response = None
            if has_doubao and doubao_driver:
                print("\n--- 豆包 Playwright ---")
                topic_text = read_topic_file(round_dir)
                if topic_text:
                    doubao_response = send_to_doubao(doubao_driver, topic_text)
                    if doubao_response:
                        resp_path = write_response_file(round_dir, "豆包", doubao_response)
                        if do_push:
                            rel = os.path.relpath(resp_path, repo_root)
                            git_add_commit_push(repo_root, [rel],
                                                f"圆桌第{round_num}轮-豆包回应")

            # --- 6. Poll for 衡 / 问 responses ---
            git_responses = {}
            if git_poll_names:
                print("\n--- 等待衡/问回应 ---")
                timeout = settings.get("response_timeout", 300)
                interval = settings.get("poll_interval", 15)
                git_responses = poll_for_responses(
                    repo_root, round_dir, git_poll_names, timeout, interval
                )

            # Also check for 默's response (written externally)
            mo_resp = read_response_file(round_dir, "默")
            if mo_resp:
                git_responses["默"] = mo_resp

            # --- 7. Compile all responses ---
            all_responses = {}
            for name in all_names:
                if name == "豆包" and doubao_response:
                    all_responses["豆包"] = doubao_response
                elif name in git_responses:
                    all_responses[name] = git_responses[name]

            if not all_responses:
                print("\n本轮没有收到任何回应。")
                round_num += 1
                continue

            show_summary(all_responses)

            # --- 8. Write summary.md and distribute ---
            print("\n--- 编写汇总 ---")
            summary_path = write_summary_file(round_dir, round_num, topic, all_responses)
            print(f"  {summary_path}")

            if do_push:
                rel = os.path.relpath(summary_path, repo_root)
                # Also push 默's response if it exists
                files = [rel]
                mo_resp_path = os.path.join(round_dir, "response_默.md")
                if os.path.exists(mo_resp_path):
                    files.append(os.path.relpath(mo_resp_path, repo_root))
                git_add_commit_push(repo_root, files,
                                    f"圆桌第{round_num}轮汇总")

            # Notify 衡/问 to pull summary
            if ide_drivers:
                print("\n--- 通知查看汇总 ---")
                notify_ide_all(ide_drivers,
                    f"圆桌第{round_num}轮汇总已推送，请 git pull 查看 "
                    f"{rounds_dir}/round_{round_num:03d}/summary.md"
                )

            # Send summary to 豆包
            if has_doubao and doubao_driver:
                print("\n--- 发汇总给豆包 ---")
                summary_text = open(summary_path, encoding="utf-8").read()
                send_to_doubao(doubao_driver, summary_text)

            # --- 9. 诚卓审阅 ---
            print("\n" + "=" * 60)
            print("本轮完成。汇总已分发给所有参与者。")
            print("=" * 60)

            previous_responses = all_responses
            round_num += 1

    except KeyboardInterrupt:
        print("\n\n被中断。")
    finally:
        if doubao_driver:
            doubao_driver.cleanup()
        print("圆桌自动化已关闭。")


if __name__ == "__main__":
    main()
