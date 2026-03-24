"""
圆桌自动化 — 基于 Git文件交换 + Playwright 的多AI协作系统。

标准流程（每个议题两轮 + 开放窗口）：
  Turn 00（发卷）：
    1. 默写 topic.md + response_默_00.md → git push
    2. 通知衡/问（点击输入框 + 粘贴）
    3. Playwright 发给豆包 → 收回应 → 写 response_豆包_00.md → push
    4. 轮询 git pull 等衡/问的 response_X_00.md
    5. 收齐后人工写 summary_00.md → push → 分发汇总

  Turn 01（交锋）：
    1. 默写 response_默_01.md → push
    2. 通知衡/问看汇总并写 _01 回应
    3. Playwright 发豆包交锋提示 → 收回应 → 写 response_豆包_01.md → push
    4. 轮询等衡/问的 _01 文件
    5. 人工写 summary_01.md → push → 分发 + 附带"本轮结束，可选补充"

用法:
    py main.py                     交互式完整流程
    py main.py --topic "议题"      直接指定议题
    py main.py --round 5           指定轮次编号
"""

import os
import sys
import time
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import yaml
import pyperclip
import pyautogui
from pywinauto import Desktop

from core.formatter import get_round_dir, read_response_file, next_turn_number
from core.git_ops import git_add_commit_push, git_pull

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3


def load_config():
    with open(os.path.join(SCRIPT_DIR, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def notify_ide(desktop, name: str, cfg: dict, message: str):
    """Send notification to an IDE participant via window activation + click + paste."""
    keyword = cfg["window_keyword"]
    for win in desktop.windows():
        if keyword in win.window_text():
            win.set_focus()
            time.sleep(2)

            if cfg.get("click_input", False):
                rect = win.rectangle()
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                x = rect.left + int(w * cfg.get("input_x_pct", 0.5))
                y = rect.top + int(h * cfg.get("input_y_pct", 0.9))
                pyautogui.click(x, y)
                time.sleep(1)

            pyperclip.copy(message)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)
            pyautogui.press("enter")
            time.sleep(0.5)
            print(f"  [{name}] 通知已发送", flush=True)
            return True

    print(f"  [{name}] 未找到窗口 (keyword='{keyword}')", flush=True)
    return False


def notify_all_ide(desktop, participants: dict, message: str, delay: float = 3.0):
    """Notify all IDE participants."""
    for name, cfg in participants.items():
        if cfg.get("driver") == "ide":
            notify_ide(desktop, name, cfg, message)
            time.sleep(delay)


def send_to_doubao(topic_text: str, doubao_cfg: dict) -> str | None:
    """Send message to Doubao, wait for response, return text."""
    from drivers.doubao import DouBaoDriver
    d = DouBaoDriver("豆包", doubao_cfg)
    d.initialize()
    ok = d.send_message(topic_text)
    print(f"  [豆包] 发送: {'成功' if ok else '失败'}", flush=True)

    resp = None
    if ok:
        print("  [豆包] 等待回应...", flush=True)
        d.wait_for_response(timeout=180)
        resp = d.get_response()
        if resp:
            print(f"  [豆包] 收到回应（{len(resp)}字）", flush=True)
        else:
            print("  [豆包] 未获取到回应", flush=True)

    d.cleanup()
    return resp


def send_doubao_no_wait(text: str, doubao_cfg: dict):
    """Send message to Doubao without waiting for response (for summary distribution)."""
    from drivers.doubao import DouBaoDriver
    d = DouBaoDriver("豆包", doubao_cfg)
    d.initialize()
    ok = d.send_message(text)
    print(f"  [豆包] 发送汇总: {'成功' if ok else '失败'}", flush=True)
    d.cleanup()


def poll_git_responses(repo_root: str, round_dir: str, names: list[str],
                       turn: int, timeout: int = 300, interval: int = 15) -> dict:
    """Poll git for response files from IDE participants."""
    print(f"\n等待回应 (turn {turn:02d})，每 {interval}s 拉取，{timeout}s 超时...", flush=True)
    start = time.time()
    collected = {}

    while time.time() - start < timeout:
        git_pull(repo_root)
        for name in names:
            if name not in collected:
                resp = read_response_file(round_dir, name, turn)
                if resp:
                    collected[name] = resp
                    print(f"  [{name}] 回应已到（{len(resp)}字）", flush=True)

        if len(collected) == len(names):
            print("所有回应已收齐！", flush=True)
            return collected

        elapsed = int(time.time() - start)
        missing = [n for n in names if n not in collected]
        print(f"  [{elapsed}s] 已收: {list(collected.keys())} | 待: {missing}", flush=True)
        time.sleep(interval)

    missing = [n for n in names if n not in collected]
    if missing:
        print(f"\n超时，未收到: {missing}", flush=True)
    return collected


def main():
    parser = argparse.ArgumentParser(description="圆桌自动化")
    parser.add_argument("--round", type=int, default=None, help="轮次编号")
    parser.add_argument("--topic", type=str, default=None, help="议题内容")
    parser.add_argument("--turn", type=int, default=0, help="从第几个turn开始 (0=发卷, 1=交锋)")
    args = parser.parse_args()

    config = load_config()
    settings = config.get("settings", {})
    participants = config["participants"]
    repo_root = settings["repo_root"]
    rounds_dir = settings["rounds_dir"]

    ide_names = [n for n, p in participants.items() if p.get("driver") == "ide"]
    doubao_cfg = {**settings, **participants.get("豆包", {})}
    has_doubao = "豆包" in participants

    desktop = Desktop(backend="uia")

    # Determine round number
    if args.round:
        round_num = args.round
    else:
        existing = []
        base = os.path.join(repo_root, rounds_dir)
        if os.path.exists(base):
            for d in os.listdir(base):
                if d.startswith("round_"):
                    try:
                        existing.append(int(d.replace("round_", "")))
                    except ValueError:
                        pass
        round_num = max(existing) + 1 if existing else 1

    round_dir = get_round_dir(repo_root, rounds_dir, round_num)

    print(f"\n{'='*60}", flush=True)
    print(f"圆桌自动化 — 第{round_num}轮", flush=True)
    print(f"参与者: {', '.join(participants.keys())}", flush=True)
    print(f"IDE通知: {', '.join(ide_names)}", flush=True)
    print(f"{'='*60}\n", flush=True)

    # ===================== TURN 00: 发卷 =====================
    if args.turn <= 0:
        print(">>> TURN 00: 发卷 <<<\n", flush=True)

        # Step 1: topic.md and 默's response should already be written
        topic_file = os.path.join(round_dir, "topic.md")
        mo_resp = os.path.join(round_dir, "response_默_00.md")
        if not os.path.exists(topic_file):
            print(f"请先写好 {topic_file} 和 {mo_resp}，然后重新运行。", flush=True)
            return

        # Step 2: Git push
        print("--- Git push 议题 ---", flush=True)
        git_add_commit_push(repo_root, [rounds_dir + f"/round_{round_num:03d}/"],
                            f"圆桌第{round_num}轮议题+默回应")

        # Step 3: Notify IDE participants
        notify_msg = (
            f"圆桌第{round_num}轮议题已推送。"
            f"请 git pull 后查看 {rounds_dir}/round_{round_num:03d}/topic.md，"
            f"写完回应后保存为 response_{{你的名字}}_00.md，然后 git add + commit + push。"
        )
        print("\n--- 通知衡/问 ---", flush=True)
        notify_all_ide(desktop, participants, notify_msg)

        # Step 4: Send to Doubao
        if has_doubao:
            print("\n--- 发给豆包 ---", flush=True)
            topic_text = open(topic_file, encoding="utf-8").read()
            resp = send_to_doubao(topic_text, doubao_cfg)
            if resp:
                out = os.path.join(round_dir, "response_豆包_00.md")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(resp)
                git_add_commit_push(repo_root, [rounds_dir + f"/round_{round_num:03d}/"],
                                    f"round{round_num}: doubao response")

        # Step 5: Poll for IDE responses
        print("\n--- 等待衡/问回应 ---", flush=True)
        poll_git_responses(repo_root, round_dir, ide_names, turn=0,
                           timeout=settings.get("response_timeout", 300),
                           interval=settings.get("poll_interval", 15))

        print("\n>>> TURN 00 收集完毕 <<<", flush=True)
        print("请手动写 summary_00.md，然后运行: py main.py --turn 1\n", flush=True)

        # Distribute summary_00 if it exists
        summary_00 = os.path.join(round_dir, "summary_00.md")
        if os.path.exists(summary_00):
            print("--- 分发 summary_00 ---", flush=True)
            git_add_commit_push(repo_root, [rounds_dir + f"/round_{round_num:03d}/"],
                                f"round{round_num}: summary_00")
            dist_msg = (
                f"圆桌第{round_num}轮汇总已出，"
                f"请 git pull 查看 {rounds_dir}/round_{round_num:03d}/summary_00.md"
            )
            notify_all_ide(desktop, participants, dist_msg)
            if has_doubao:
                summary_text = open(summary_00, encoding="utf-8").read()
                send_doubao_no_wait(f"以下是第{round_num}轮汇总，请查阅：\n\n" + summary_text,
                                    doubao_cfg)

    # ===================== TURN 01: 交锋 =====================
    if args.turn <= 1:
        print("\n>>> TURN 01: 交锋 <<<\n", flush=True)

        # 默's turn 01 response should already be written
        mo_01 = os.path.join(round_dir, "response_默_01.md")
        if not os.path.exists(mo_01):
            print(f"请先写好 {mo_01}，然后重新运行 --turn 1。", flush=True)
            return

        # Git push 默's turn 01
        git_add_commit_push(repo_root, [rounds_dir + f"/round_{round_num:03d}/"],
                            f"round{round_num} turn01: mo response")

        # Notify IDE for turn 01
        notify_msg = (
            f"圆桌第{round_num}轮第二回合——汇总已出，"
            f"请 git pull 查看 summary_00.md 和其他人的回应，"
            f"然后写 response_{{你的名字}}_01.md，git add + commit + push。"
        )
        print("--- 通知衡/问 turn 01 ---", flush=True)
        notify_all_ide(desktop, participants, notify_msg)

        # Send to Doubao for turn 01
        if has_doubao:
            print("\n--- 发给豆包 turn 01 ---", flush=True)
            summary_00 = os.path.join(round_dir, "summary_00.md")
            doubao_prompt = "第二回合。请看完汇总后回应其他人对你的看法，畅所欲言。"
            if os.path.exists(summary_00):
                doubao_prompt = open(summary_00, encoding="utf-8").read() + "\n\n" + doubao_prompt
            resp = send_to_doubao(doubao_prompt, doubao_cfg)
            if resp:
                out = os.path.join(round_dir, "response_豆包_01.md")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(resp)
                git_add_commit_push(repo_root, [rounds_dir + f"/round_{round_num:03d}/"],
                                    f"round{round_num} turn01: doubao response")

        # Poll for turn 01
        print("\n--- 等待衡/问 turn 01 ---", flush=True)
        poll_git_responses(repo_root, round_dir, ide_names, turn=1,
                           timeout=settings.get("response_timeout", 300),
                           interval=settings.get("poll_interval", 15))

        print("\n>>> TURN 01 收集完毕 <<<", flush=True)
        print("请手动写 summary_01.md，然后运行: py main.py --turn 2\n", flush=True)

    # ===================== CLOSE: 分发最终汇总 + 结束通知 =====================
    if args.turn <= 2:
        summary_01 = os.path.join(round_dir, "summary_01.md")
        if os.path.exists(summary_01):
            print("\n>>> 分发最终汇总 + 结束通知 <<<\n", flush=True)
            git_add_commit_push(repo_root, [rounds_dir + f"/round_{round_num:03d}/"],
                                f"round{round_num} turn01: summary")

            close_msg = (
                f"圆桌第{round_num}轮最终汇总已出，"
                f"请 git pull 查看 {rounds_dir}/round_{round_num:03d}/summary_01.md。"
                f"本轮议题到此结束。如有补充可写 response_{{你的名字}}_02.md 提交，但不强制。"
            )
            print("--- 通知衡/问（结束） ---", flush=True)
            notify_all_ide(desktop, participants, close_msg)

            if has_doubao:
                print("--- 发给豆包（结束） ---", flush=True)
                summary_text = open(summary_01, encoding="utf-8").read()
                closing = (
                    "\n\n---\n本轮议题到此结束。如果你还想补充什么，可以告诉诚卓，"
                    "但不强制回复。"
                )
                send_doubao_no_wait(
                    f"以下是最终汇总：\n\n{summary_text}{closing}",
                    doubao_cfg
                )

            print(f"\n第{round_num}轮圆桌完成。\n", flush=True)
        else:
            print(f"未找到 summary_01.md，请先写好再运行 --turn 2。", flush=True)


if __name__ == "__main__":
    main()
