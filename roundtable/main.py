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
import json
import argparse
from datetime import datetime

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
# Response status tracking
# ---------------------------------------------------------------------------

RESPONSE_STATUS = {}

def set_response_status(name: str, turn: int, status: str, char_count: int = 0):
    """Record response status: ok / extraction_error / timeout / missing / silence."""
    key = f"{name}_turn{turn:02d}"
    RESPONSE_STATUS[key] = {
        "name": name, "turn": turn, "status": status,
        "chars": char_count, "time": datetime.now().isoformat(),
    }

def save_response_status(round_dir: str):
    """Write response_status.json to the round directory."""
    path = os.path.join(round_dir, "response_status.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(RESPONSE_STATUS, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Operation log
# ---------------------------------------------------------------------------

RUN_LOG_LINES = []

def log(msg: str):
    """Append a timestamped line to the operation log and print it."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    RUN_LOG_LINES.append(line)
    print(line, flush=True)

def save_run_log(round_dir: str, turn: int):
    """Write run_log_NN.md to the round directory."""
    path = os.path.join(round_dir, f"run_log_{turn:02d}.md")
    header = f"# 操作日志 — Turn {turn:02d}\n*{datetime.now().isoformat()}*\n\n"
    body = "\n".join(RUN_LOG_LINES)

    status_summary = "\n\n## 回应状态\n\n| 参与者 | Turn | 状态 | 字数 |\n|--------|------|------|------|\n"
    for entry in RESPONSE_STATUS.values():
        status_summary += f"| {entry['name']} | {entry['turn']:02d} | {entry['status']} | {entry['chars']} |\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body + status_summary)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    with open(os.path.join(SCRIPT_DIR, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def rotated_order(order: list[str], round_num: int) -> list[str]:
    """Rotate participant order by round number so no one is always last."""
    if not order:
        return order
    shift = (round_num - 1) % len(order)
    return order[shift:] + order[:shift]


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
            log(f"[{name}] 通知已发送")
            return True

    log(f"[{name}] 未找到窗口 (keyword='{keyword}')")
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

def send_to_doubao(topic_text: str, doubao_cfg: dict,
                    turn: int = -1) -> tuple[str | None, str]:
    """Send message to Doubao, wait for response.
    Returns (response_text, status) where status is 'ok' / 'extraction_error' / 'send_failed'.
    """
    from drivers.doubao import DouBaoDriver
    d = DouBaoDriver("豆包", doubao_cfg)
    d.initialize()
    ok = d.send_message(topic_text)
    log(f"[豆包] 发送: {'成功' if ok else '失败'}")

    resp = None
    status = "send_failed"
    if ok:
        log("[豆包] 等待回应...")
        d.wait_for_response(timeout=180)
        resp = d.get_response()
        if resp:
            log(f"[豆包] 收到回应（{len(resp)}字）")
            status = "ok"
            set_response_status("豆包", turn, "ok", len(resp))
        else:
            log("[豆包] 未获取到回应（extraction_error）")
            status = "extraction_error"
            set_response_status("豆包", turn, "extraction_error", 0)
    else:
        set_response_status("豆包", turn, "send_failed", 0)

    d.cleanup()
    return resp, status


def send_doubao_no_wait(text: str, doubao_cfg: dict):
    """Send message to Doubao without waiting for response."""
    from drivers.doubao import DouBaoDriver
    d = DouBaoDriver("豆包", doubao_cfg)
    d.initialize()
    ok = d.send_message(text)
    log(f"[豆包] 发送（无等待）: {'成功' if ok else '失败'}")
    d.cleanup()


# ---------------------------------------------------------------------------
# Doubao confirmation prefix
# ---------------------------------------------------------------------------

_doubao_last_status = {"turn": -1, "status": "none", "chars": 0}

def _doubao_confirm_prefix() -> str:
    """Build a confirmation prefix for Doubao based on last response status."""
    s = _doubao_last_status
    if s["status"] == "ok":
        return f"【确认】你上一轮（Turn {s['turn']:02d}）的回应已完整记录，共 {s['chars']} 字。如有遗漏请告知。\n\n"
    if s["status"] == "extraction_error":
        return (f"【注意】你上一轮（Turn {s['turn']:02d}）的回应提取失败，"
                "系统未能记录你的完整发言。如果你还记得要点，可以在这轮补充。\n\n")
    return ""


# ---------------------------------------------------------------------------
# Git poll with silence detection
# ---------------------------------------------------------------------------

def poll_git_responses(repo_root: str, round_dir: str, names: list[str],
                       turn: int, timeout: int = 300, interval: int = 15,
                       silence_keywords: list[str] | None = None) -> dict:
    """Poll git for response files. Returns dict of {name: text}.
    Silence declarations are collected as valid responses.
    """
    log(f"等待回应 (turn {turn:02d})，每 {interval}s 拉取，{timeout}s 超时...")
    start = time.time()
    collected = {}

    while time.time() - start < timeout:
        git_pull(repo_root)
        for name in names:
            if name not in collected:
                resp = read_response_file(round_dir, name, turn)
                if resp:
                    if is_silence_declaration(resp, silence_keywords):
                        log(f"[{name}] 沉默声明（本轮不发言）")
                        set_response_status(name, turn, "silence", len(resp))
                    else:
                        log(f"[{name}] 回应已到（{len(resp)}字）")
                        set_response_status(name, turn, "ok", len(resp))
                    collected[name] = resp

        if len(collected) == len(names):
            log("所有回应已收齐！")
            return collected

        elapsed = int(time.time() - start)
        missing = [n for n in names if n not in collected]
        print(f"  [{elapsed}s] 已收: {list(collected.keys())} | 待: {missing}", flush=True)
        time.sleep(interval)

    missing = [n for n in names if n not in collected]
    if missing:
        log(f"超时，未收到: {missing}")
        for name in missing:
            set_response_status(name, turn, "timeout", 0)
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
    log(f">>> TURN {turn:02d}: 追加轮 <<<")

    ide_names = [n for n, p in participants.items() if p.get("driver") == "ide"]
    silence_kw = settings.get("silence_keywords", SILENCE_KEYWORDS_DEFAULT)
    compress_after = settings.get("doubao_compress_after_turn", 1)

    mo_file = os.path.join(round_dir, f"response_默_{turn:02d}.md")
    if not os.path.exists(mo_file):
        log(f"请先写好 {mo_file}（没有想说的就写'这轮我没有需要发言的地方'），然后继续。")
        return False

    mo_text = open(mo_file, encoding="utf-8").read()
    if is_silence_declaration(mo_text, silence_kw):
        set_response_status("默", turn, "silence", len(mo_text))
    else:
        set_response_status("默", turn, "ok", len(mo_text))

    round_path = rounds_dir + f"/round_{round_num:03d}/"
    git_add_commit_push(repo_root, [round_path],
                        f"round{round_num} turn{turn:02d}: mo response")

    notify_msg = make_notify_msg_extra_turn(round_num, turn, rounds_dir)
    log(f"--- 通知衡/问 turn {turn:02d} ---")
    notify_all_ide(desktop, participants, notify_msg)

    if has_doubao:
        log(f"--- 发给豆包 turn {turn:02d} ---")
        prev_summary = os.path.join(round_dir, f"summary_{turn - 1:02d}.md")
        doubao_input = get_doubao_input(round_dir, prev_summary, turn, compress_after)
        if doubao_input:
            confirm = _doubao_confirm_prefix()
            prompt = confirm + doubao_input + "\n\n" + make_doubao_extra_turn_prompt(turn)
            resp, status = send_to_doubao(prompt, doubao_cfg, turn=turn)
            _doubao_last_status.update({"turn": turn, "status": status,
                                         "chars": len(resp) if resp else 0})
            if resp:
                out = os.path.join(round_dir, f"response_豆包_{turn:02d}.md")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(resp)
                git_add_commit_push(repo_root, [round_path],
                                    f"round{round_num} turn{turn:02d}: doubao response")

    log(f"--- 等待衡/问 turn {turn:02d} ---")
    poll_git_responses(repo_root, round_dir, ide_names, turn=turn,
                       timeout=settings.get("response_timeout", 300),
                       interval=settings.get("poll_interval", 15),
                       silence_keywords=silence_kw)

    save_response_status(round_dir)
    save_run_log(round_dir, turn)
    log(f">>> TURN {turn:02d} 收集完毕 <<<")
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
        log(f"未找到 summary_{turn:02d}.md，请先写好。")
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

    log(f"--- 通知衡/问 ({'结束' if is_final else '汇总'}) ---")
    notify_all_ide(desktop, participants, ide_msg)

    if has_doubao:
        log(f"--- 发给豆包 ({'结束' if is_final else '汇总'}) ---")
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

def run_notify(targets: list[str], message: str):
    """Send a one-way notification to specified participants."""
    config = load_config()
    participants = config["participants"]
    settings = config.get("settings", {})
    desktop = Desktop(backend="uia")

    all_names = list(participants.keys())
    if not targets:
        targets = [n for n, p in participants.items() if p.get("driver") == "ide"]

    for name in targets:
        if name not in participants:
            log(f"[{name}] 未找到该参与者，跳过")
            continue
        cfg = participants[name]
        driver = cfg.get("driver")

        if driver == "ide":
            notify_ide(desktop, name, cfg, message)
        elif driver == "doubao":
            doubao_cfg = {**settings, **cfg}
            send_doubao_no_wait(message, doubao_cfg)
        else:
            log(f"[{name}] driver={driver}，不支持通知，跳过")

        time.sleep(3)

    log("通知发送完毕。")


def main():
    global config_settings_cache

    parser = argparse.ArgumentParser(description="圆桌自动化")
    parser.add_argument("--round", type=int, default=None, help="轮次编号")
    parser.add_argument("--turn", type=int, default=0, help="从第几个turn开始")
    parser.add_argument("--notify", type=str, default=None,
                        help="单向通知模式：发送消息给指定参与者")
    parser.add_argument("--to", type=str, default=None,
                        help="通知目标，逗号分隔（默认所有IDE参与者）。如：衡,问")
    args = parser.parse_args()

    if args.notify:
        targets = [t.strip() for t in args.to.split(",")] if args.to else []
        run_notify(targets, args.notify)
        return

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

    order = settings.get("default_order", list(participants.keys()))
    if settings.get("rotate_order", False):
        order = rotated_order(order, round_num)

    log(f"{'='*60}")
    log(f"圆桌自动化 — 第{round_num}轮")
    log(f"参与者: {', '.join(participants.keys())}")
    log(f"本轮顺序: {' → '.join(order)}")
    log(f"IDE通知: {', '.join(ide_names)}")
    log(f"豆包压缩阈值: turn > {settings.get('doubao_compress_after_turn', 1)}")
    log(f"{'='*60}")

    # ===================== TURN 00: 发卷 =====================
    if args.turn <= 0:
        log(">>> TURN 00: 发卷 <<<")

        topic_file = os.path.join(round_dir, "topic.md")
        if not os.path.exists(topic_file):
            log(f"请先写好 {topic_file} 和 response_默_00.md，然后重新运行。")
            return

        set_response_status("默", 0, "ok",
                            len(open(os.path.join(round_dir, "response_默_00.md"),
                                     encoding="utf-8").read())
                            if os.path.exists(os.path.join(round_dir, "response_默_00.md")) else 0)

        log("--- Git push 议题 ---")
        git_add_commit_push(repo_root, [round_path],
                            f"圆桌第{round_num}轮议题+默回应")

        notify_msg = make_notify_msg_turn00(round_num, rounds_dir)
        log("--- 通知衡/问 ---")
        notify_all_ide(desktop, participants, notify_msg)

        if has_doubao:
            log("--- 发给豆包 ---")
            topic_text = open(topic_file, encoding="utf-8").read()
            resp, status = send_to_doubao(topic_text, doubao_cfg, turn=0)
            _doubao_last_status.update({"turn": 0, "status": status,
                                         "chars": len(resp) if resp else 0})
            if resp:
                out = os.path.join(round_dir, "response_豆包_00.md")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(resp)
                git_add_commit_push(repo_root, [round_path],
                                    f"round{round_num}: doubao response")

        log("--- 等待衡/问回应 ---")
        poll_git_responses(repo_root, round_dir, ide_names, turn=0,
                           timeout=settings.get("response_timeout", 300),
                           interval=settings.get("poll_interval", 15),
                           silence_keywords=silence_kw)

        save_response_status(round_dir)
        save_run_log(round_dir, 0)
        log(">>> TURN 00 收集完毕 <<<")
        print("请手动写 summary_00.md，然后运行: py main.py --turn 1\n", flush=True)

        summary_00 = os.path.join(round_dir, "summary_00.md")
        if os.path.exists(summary_00):
            distribute_summary(0, round_num, round_dir, rounds_dir, repo_root,
                               desktop, participants, doubao_cfg, has_doubao,
                               is_final=False)

    # ===================== TURN 01: 交锋 =====================
    if args.turn <= 1:
        log(">>> TURN 01: 交锋 <<<")

        mo_01 = os.path.join(round_dir, "response_默_01.md")
        if not os.path.exists(mo_01):
            log(f"请先写好 {mo_01}，然后重新运行 --turn 1。")
            return

        set_response_status("默", 1, "ok",
                            len(open(mo_01, encoding="utf-8").read()))

        git_add_commit_push(repo_root, [round_path],
                            f"round{round_num} turn01: mo response")

        notify_msg = make_notify_msg_turn01(round_num)
        log("--- 通知衡/问 turn 01 ---")
        notify_all_ide(desktop, participants, notify_msg)

        if has_doubao:
            log("--- 发给豆包 turn 01 ---")
            summary_00 = os.path.join(round_dir, "summary_00.md")
            confirm = _doubao_confirm_prefix()
            doubao_prompt = confirm + "第二回合。请看完汇总后回应其他人对你的看法，畅所欲言。"
            if os.path.exists(summary_00):
                doubao_prompt = confirm + open(summary_00, encoding="utf-8").read() + \
                    "\n\n第二回合。请看完汇总后回应其他人对你的看法，畅所欲言。"
            resp, status = send_to_doubao(doubao_prompt, doubao_cfg, turn=1)
            _doubao_last_status.update({"turn": 1, "status": status,
                                         "chars": len(resp) if resp else 0})
            if resp:
                out = os.path.join(round_dir, "response_豆包_01.md")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(resp)
                git_add_commit_push(repo_root, [round_path],
                                    f"round{round_num} turn01: doubao response")

        log("--- 等待衡/问 turn 01 ---")
        poll_git_responses(repo_root, round_dir, ide_names, turn=1,
                           timeout=settings.get("response_timeout", 300),
                           interval=settings.get("poll_interval", 15),
                           silence_keywords=silence_kw)

        save_response_status(round_dir)
        save_run_log(round_dir, 1)
        log(">>> TURN 01 收集完毕 <<<")
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
