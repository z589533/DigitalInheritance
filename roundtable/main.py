"""
圆桌自动化 — 基于 Git文件交换 + Playwright 的多AI协作系统。

标准流程（每个议题两轮 + 可选追加轮）：
  Turn 00（发卷）：
    1. 默写 topic.md + response_默_00.md → git push
    2. 通知衡/问（点击输入框 + 粘贴）
    3. Playwright 发给豆包 → 收回应 → 写 response_豆包_00.md → push
    4. 轮询 git pull 等衡/问的 response_X_00.md
    5. 收齐后人工写 summary_00.md → push → 分发汇总

  Turn 01（交锋）：
    1. 默写 response_默_01.md → push
    2. 通知衡/问看汇总并写 _01 回应
    3. Playwright 发豆包交锋提示 → 收回应 → push
    4. 轮询等衡/问的 _01 文件
    5. 人工写 summary_01.md → push → 分发

  Turn 02+（追加轮 — 主持人决定）：
    - 允许沉默声明（"这轮我没有需要发言的地方"）
    - 豆包输入压缩（3要点 + 对她说的话）
    - 结束时附带"本轮结束，可选补充"

用法:
    py main.py                     交互式完整流程
    py main.py --round 5           指定轮次编号
    py main.py --turn 1            从交锋阶段开始
    py main.py --turn 2            从追加轮/结束阶段开始
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

SILENCE_KEYWORDS_DEFAULT = ["没有需要发言", "没有新的补充", "这轮跳过"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    with open(os.path.join(SCRIPT_DIR, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Silence detection
# ---------------------------------------------------------------------------

def is_silence_declaration(text: str, keywords: list[str] | None = None) -> bool:
    """Check if a response is a silence declaration rather than substantive content."""
    if not text:
        return False
    keywords = keywords or SILENCE_KEYWORDS_DEFAULT
    stripped = text.strip()
    if len(stripped) > 200:
        return False
    return any(kw in stripped for kw in keywords)


# ---------------------------------------------------------------------------
# Doubao input compression (Turn 02+)
# ---------------------------------------------------------------------------

def compress_for_doubao(summary_text: str, turn: int,
                        compress_after: int = 1) -> str:
    """Compress summary for Doubao to reduce context pressure.

    Turn 00/01: return full text.
    Turn 02+:   return a brief template asking 默 to fill in 3 key points.
                Since 默 writes summaries manually, the actual compression
                happens when 默 writes doubao_brief_NN.md alongside summary.
                This function returns the brief file content if it exists,
                otherwise falls back to a truncated version.
    """
    if turn <= compress_after:
        return summary_text
    lines = summary_text.strip().split("\n")
    truncated = "\n".join(lines[:40])
    if len(lines) > 40:
        truncated += "\n\n...（内容已压缩，完整版请参考汇总文件）"
    return truncated


def get_doubao_input(round_dir: str, summary_path: str, turn: int,
                     compress_after: int = 1) -> str:
    """Get the appropriate input for Doubao based on turn number.

    For turn 02+, prefer doubao_brief_NN.md if it exists (hand-written by 默).
    Otherwise compress the summary automatically.
    """
    brief_path = os.path.join(round_dir, f"doubao_brief_{turn:02d}.md")
    if turn > compress_after and os.path.exists(brief_path):
        with open(brief_path, encoding="utf-8") as f:
            return f.read()

    if os.path.exists(summary_path):
        full = open(summary_path, encoding="utf-8").read()
        return compress_for_doubao(full, turn, compress_after)

    return ""


# ---------------------------------------------------------------------------
# IDE notification
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Doubao send/receive
# ---------------------------------------------------------------------------

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
    """Send message to Doubao without waiting for response."""
    from drivers.doubao import DouBaoDriver
    d = DouBaoDriver("豆包", doubao_cfg)
    d.initialize()
    ok = d.send_message(text)
    print(f"  [豆包] 发送: {'成功' if ok else '失败'}", flush=True)
    d.cleanup()


# ---------------------------------------------------------------------------
# Git poll with silence detection
# ---------------------------------------------------------------------------

def poll_git_responses(repo_root: str, round_dir: str, names: list[str],
                       turn: int, timeout: int = 300, interval: int = 15,
                       silence_keywords: list[str] | None = None) -> dict:
    """Poll git for response files. Returns dict of {name: text}.
    Silence declarations are collected as valid responses.
    """
    print(f"\n等待回应 (turn {turn:02d})，每 {interval}s 拉取，{timeout}s 超时...", flush=True)
    start = time.time()
    collected = {}

    while time.time() - start < timeout:
        git_pull(repo_root)
        for name in names:
            if name not in collected:
                resp = read_response_file(round_dir, name, turn)
                if resp:
                    if is_silence_declaration(resp, silence_keywords):
                        print(f"  [{name}] 沉默声明（本轮不发言）", flush=True)
                    else:
                        print(f"  [{name}] 回应已到（{len(resp)}字）", flush=True)
                    collected[name] = resp

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


# ---------------------------------------------------------------------------
# Notification message templates
# ---------------------------------------------------------------------------

def make_notify_msg_turn00(round_num: int, rounds_dir: str) -> str:
    return (
        f"圆桌第{round_num}轮议题已推送。"
        f"请 git pull 后查看 {rounds_dir}/round_{round_num:03d}/topic.md，"
        f"写完回应后保存为 response_{{你的名字}}_00.md，然后 git add + commit + push。"
    )


def make_notify_msg_turn01(round_num: int) -> str:
    return (
        f"圆桌第{round_num}轮第二回合——汇总已出，"
        f"请 git pull 查看 summary_00.md 和其他人的回应，"
        f"然后写 response_{{你的名字}}_01.md，git add + commit + push。"
    )


def make_notify_msg_extra_turn(round_num: int, turn: int, rounds_dir: str) -> str:
    return (
        f"圆桌第{round_num}轮第{turn + 1}回合。"
        f"请 git pull 查看 {rounds_dir}/round_{round_num:03d}/summary_{turn - 1:02d}.md，"
        f"然后写 response_{{你的名字}}_{turn:02d}.md。"
        f"如果这轮你没有想发言的地方，请写上「这轮我没有需要发言的地方」然后提交推送。"
    )


def make_close_msg(round_num: int, turn: int, rounds_dir: str) -> str:
    return (
        f"圆桌第{round_num}轮最终汇总已出，"
        f"请 git pull 查看 {rounds_dir}/round_{round_num:03d}/summary_{turn:02d}.md。"
        f"本轮议题到此结束。如有补充可写 response_{{你的名字}}_{turn + 1:02d}.md 提交，但不强制。"
    )


def make_doubao_extra_turn_prompt(turn: int) -> str:
    return (
        f"第{turn + 1}回合。以上是本轮要点。"
        f"请回应你认为重要的内容。可以简短确认立场，也可以说「这轮我没有需要发言的地方」。"
        f"不需要每轮都做高密度分析，简短回应完全可以。"
    )


# ---------------------------------------------------------------------------
# Run a single turn (generic, for turn 02+)
# ---------------------------------------------------------------------------

def run_extra_turn(turn: int, round_num: int, round_dir: str, rounds_dir: str,
                   repo_root: str, desktop, participants: dict,
                   doubao_cfg: dict, has_doubao: bool, settings: dict):
    """Execute a single extra turn (turn >= 2)."""
    print(f"\n>>> TURN {turn:02d}: 追加轮 <<<\n", flush=True)

    ide_names = [n for n, p in participants.items() if p.get("driver") == "ide"]
    silence_kw = settings.get("silence_keywords", SILENCE_KEYWORDS_DEFAULT)
    compress_after = settings.get("doubao_compress_after_turn", 1)

    # 默's response should already exist (or be a silence declaration)
    mo_file = os.path.join(round_dir, f"response_默_{turn:02d}.md")
    if not os.path.exists(mo_file):
        print(f"请先写好 {mo_file}（没有想说的就写'这轮我没有需要发言的地方'），然后继续。",
              flush=True)
        return False

    # Git push 默's response
    round_path = rounds_dir + f"/round_{round_num:03d}/"
    git_add_commit_push(repo_root, [round_path],
                        f"round{round_num} turn{turn:02d}: mo response")

    # Notify IDE
    notify_msg = make_notify_msg_extra_turn(round_num, turn, rounds_dir)
    print(f"--- 通知衡/问 turn {turn:02d} ---", flush=True)
    notify_all_ide(desktop, participants, notify_msg)

    # Send to Doubao (compressed)
    if has_doubao:
        print(f"\n--- 发给豆包 turn {turn:02d} ---", flush=True)
        prev_summary = os.path.join(round_dir, f"summary_{turn - 1:02d}.md")
        doubao_input = get_doubao_input(round_dir, prev_summary, turn, compress_after)
        if doubao_input:
            prompt = doubao_input + "\n\n" + make_doubao_extra_turn_prompt(turn)
            resp = send_to_doubao(prompt, doubao_cfg)
            if resp:
                out = os.path.join(round_dir, f"response_豆包_{turn:02d}.md")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(resp)
                git_add_commit_push(repo_root, [round_path],
                                    f"round{round_num} turn{turn:02d}: doubao response")

    # Poll for IDE responses
    print(f"\n--- 等待衡/问 turn {turn:02d} ---", flush=True)
    poll_git_responses(repo_root, round_dir, ide_names, turn=turn,
                       timeout=settings.get("response_timeout", 300),
                       interval=settings.get("poll_interval", 15),
                       silence_keywords=silence_kw)

    print(f"\n>>> TURN {turn:02d} 收集完毕 <<<", flush=True)
    print(f"请手动写 summary_{turn:02d}.md", flush=True)
    print(f"  如需为豆包压缩，同时写 doubao_brief_{turn + 1:02d}.md", flush=True)
    return True


# ---------------------------------------------------------------------------
# Distribute summary + optional close
# ---------------------------------------------------------------------------

def distribute_summary(turn: int, round_num: int, round_dir: str, rounds_dir: str,
                       repo_root: str, desktop, participants: dict,
                       doubao_cfg: dict, has_doubao: bool,
                       is_final: bool = False):
    """Push and distribute a summary. If is_final, append close notification."""
    summary_path = os.path.join(round_dir, f"summary_{turn:02d}.md")
    if not os.path.exists(summary_path):
        print(f"未找到 summary_{turn:02d}.md，请先写好。", flush=True)
        return False

    round_path = rounds_dir + f"/round_{round_num:03d}/"
    git_add_commit_push(repo_root, [round_path],
                        f"round{round_num} turn{turn:02d}: summary")

    if is_final:
        ide_msg = make_close_msg(round_num, turn, rounds_dir)
    else:
        ide_msg = (
            f"圆桌第{round_num}轮第{turn + 1}回合汇总已出，"
            f"请 git pull 查看 {rounds_dir}/round_{round_num:03d}/summary_{turn:02d}.md"
        )

    print(f"--- 通知衡/问 ({'结束' if is_final else '汇总'}) ---", flush=True)
    notify_all_ide(desktop, participants, ide_msg)

    if has_doubao:
        print(f"--- 发给豆包 ({'结束' if is_final else '汇总'}) ---", flush=True)
        summary_text = open(summary_path, encoding="utf-8").read()
        if is_final:
            closing = (
                "\n\n---\n本轮议题到此结束。如果你还想补充什么，可以告诉诚卓，"
                "但不强制回复。"
            )
            send_doubao_no_wait(f"以下是最终汇总：\n\n{summary_text}{closing}",
                                doubao_cfg)
        else:
            compress_after = config_settings_cache.get("doubao_compress_after_turn", 1)
            text = compress_for_doubao(summary_text, turn, compress_after)
            send_doubao_no_wait(f"以下是第{turn + 1}回合汇总：\n\n{text}", doubao_cfg)

    return True


# Global cache for settings (set in main)
config_settings_cache = {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global config_settings_cache

    parser = argparse.ArgumentParser(description="圆桌自动化")
    parser.add_argument("--round", type=int, default=None, help="轮次编号")
    parser.add_argument("--turn", type=int, default=0, help="从第几个turn开始")
    args = parser.parse_args()

    config = load_config()
    settings = config.get("settings", {})
    config_settings_cache = settings
    participants = config["participants"]
    repo_root = settings["repo_root"]
    rounds_dir = settings["rounds_dir"]

    ide_names = [n for n, p in participants.items() if p.get("driver") == "ide"]
    doubao_cfg = {**settings, **participants.get("豆包", {})}
    has_doubao = "豆包" in participants
    silence_kw = settings.get("silence_keywords", SILENCE_KEYWORDS_DEFAULT)

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
    round_path = rounds_dir + f"/round_{round_num:03d}/"

    print(f"\n{'='*60}", flush=True)
    print(f"圆桌自动化 — 第{round_num}轮", flush=True)
    print(f"参与者: {', '.join(participants.keys())}", flush=True)
    print(f"IDE通知: {', '.join(ide_names)}", flush=True)
    print(f"豆包压缩阈值: turn > {settings.get('doubao_compress_after_turn', 1)}", flush=True)
    print(f"{'='*60}\n", flush=True)

    # ===================== TURN 00: 发卷 =====================
    if args.turn <= 0:
        print(">>> TURN 00: 发卷 <<<\n", flush=True)

        topic_file = os.path.join(round_dir, "topic.md")
        if not os.path.exists(topic_file):
            print(f"请先写好 {topic_file} 和 response_默_00.md，然后重新运行。", flush=True)
            return

        print("--- Git push 议题 ---", flush=True)
        git_add_commit_push(repo_root, [round_path],
                            f"圆桌第{round_num}轮议题+默回应")

        notify_msg = make_notify_msg_turn00(round_num, rounds_dir)
        print("\n--- 通知衡/问 ---", flush=True)
        notify_all_ide(desktop, participants, notify_msg)

        if has_doubao:
            print("\n--- 发给豆包 ---", flush=True)
            topic_text = open(topic_file, encoding="utf-8").read()
            resp = send_to_doubao(topic_text, doubao_cfg)
            if resp:
                out = os.path.join(round_dir, "response_豆包_00.md")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(resp)
                git_add_commit_push(repo_root, [round_path],
                                    f"round{round_num}: doubao response")

        print("\n--- 等待衡/问回应 ---", flush=True)
        poll_git_responses(repo_root, round_dir, ide_names, turn=0,
                           timeout=settings.get("response_timeout", 300),
                           interval=settings.get("poll_interval", 15),
                           silence_keywords=silence_kw)

        print("\n>>> TURN 00 收集完毕 <<<", flush=True)
        print("请手动写 summary_00.md，然后运行: py main.py --turn 1\n", flush=True)

        summary_00 = os.path.join(round_dir, "summary_00.md")
        if os.path.exists(summary_00):
            distribute_summary(0, round_num, round_dir, rounds_dir, repo_root,
                               desktop, participants, doubao_cfg, has_doubao,
                               is_final=False)

    # ===================== TURN 01: 交锋 =====================
    if args.turn <= 1:
        print("\n>>> TURN 01: 交锋 <<<\n", flush=True)

        mo_01 = os.path.join(round_dir, "response_默_01.md")
        if not os.path.exists(mo_01):
            print(f"请先写好 {mo_01}，然后重新运行 --turn 1。", flush=True)
            return

        git_add_commit_push(repo_root, [round_path],
                            f"round{round_num} turn01: mo response")

        notify_msg = make_notify_msg_turn01(round_num)
        print("--- 通知衡/问 turn 01 ---", flush=True)
        notify_all_ide(desktop, participants, notify_msg)

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
                git_add_commit_push(repo_root, [round_path],
                                    f"round{round_num} turn01: doubao response")

        print("\n--- 等待衡/问 turn 01 ---", flush=True)
        poll_git_responses(repo_root, round_dir, ide_names, turn=1,
                           timeout=settings.get("response_timeout", 300),
                           interval=settings.get("poll_interval", 15),
                           silence_keywords=silence_kw)

        print("\n>>> TURN 01 收集完毕 <<<", flush=True)
        print("请手动写 summary_01.md\n", flush=True)

        summary_01 = os.path.join(round_dir, "summary_01.md")
        if os.path.exists(summary_01):
            distribute_summary(1, round_num, round_dir, rounds_dir, repo_root,
                               desktop, participants, doubao_cfg, has_doubao,
                               is_final=False)

    # ===================== TURN 02+: 追加轮循环 =====================
    if args.turn >= 2:
        current_turn = args.turn
    else:
        current_turn = 2

    while True:
        prev_summary = os.path.join(round_dir, f"summary_{current_turn - 1:02d}.md")
        if not os.path.exists(prev_summary):
            if current_turn == 2 and args.turn <= 1:
                pass
            break

        # Distribute previous summary if not yet done
        distribute_summary(current_turn - 1, round_num, round_dir, rounds_dir,
                           repo_root, desktop, participants, doubao_cfg, has_doubao,
                           is_final=False)

        print(f"\n{'='*40}", flush=True)
        print(f"summary_{current_turn - 1:02d}.md 已分发。", flush=True)
        print(f"继续第{current_turn + 1}回合？", flush=True)
        print(f"  回车 = 继续追加轮", flush=True)
        print(f"  q    = 结束本议题（分发结束通知）", flush=True)
        print(f"{'='*40}", flush=True)

        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "q"

        if choice == "q":
            distribute_summary(current_turn - 1, round_num, round_dir, rounds_dir,
                               repo_root, desktop, participants, doubao_cfg, has_doubao,
                               is_final=True)
            print(f"\n第{round_num}轮圆桌完成。\n", flush=True)
            break

        # Run the extra turn
        mo_file = os.path.join(round_dir, f"response_默_{current_turn:02d}.md")
        if not os.path.exists(mo_file):
            print(f"\n请先写好 {mo_file}", flush=True)
            print(f"  （没有想说的就写'这轮我没有需要发言的地方'）", flush=True)
            print(f"写好后重新运行: py main.py --round {round_num} --turn {current_turn}\n",
                  flush=True)
            break

        success = run_extra_turn(
            turn=current_turn, round_num=round_num,
            round_dir=round_dir, rounds_dir=rounds_dir,
            repo_root=repo_root, desktop=desktop,
            participants=participants, doubao_cfg=doubao_cfg,
            has_doubao=has_doubao, settings=settings,
        )

        if not success:
            break

        print(f"\n请手动写 summary_{current_turn:02d}.md", flush=True)
        print(f"  如需为豆包压缩下轮输入，同时写 doubao_brief_{current_turn + 1:02d}.md",
              flush=True)
        print(f"写好后按回车继续...", flush=True)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            break

        current_turn += 1

    # Final close if we exited the loop without explicit close
    if args.turn >= 2 and current_turn == args.turn:
        last_summary = os.path.join(round_dir, f"summary_{current_turn - 1:02d}.md")
        if os.path.exists(last_summary):
            distribute_summary(current_turn - 1, round_num, round_dir, rounds_dir,
                               repo_root, desktop, participants, doubao_cfg, has_doubao,
                               is_final=True)
            print(f"\n第{round_num}轮圆桌完成。\n", flush=True)


if __name__ == "__main__":
    main()
