"""File-based round management for roundtable discussions.

Each round lives in:
    roundtable/rounds/round_NNN/
        topic.md                <- compiled topic + previous responses + git instructions
        response_默_00.md       <- first response
        response_默_01.md       <- follow-up after summary
        response_衡_00.md
        response_豆包_00.md
        response_问_00.md
        summary_00.md           <- first summary
        summary_01.md           <- follow-up summary (if any)
"""

import os
import glob
from datetime import datetime

def _git_instructions(turn: int) -> str:
    suffix = f"_{turn:02d}"
    return f"""---

【操作提示 — 衡/问 请按此执行】

1. 阅读上方内容后，将你的回应写入本目录的 `response_{{你的名字}}{suffix}.md`
   - 衡 → `response_衡{suffix}.md`
   - 问 → `response_问{suffix}.md`
2. 写完后执行：
   ```
   git add .
   git commit -m "圆桌回应"
   git push
   ```
3. 提交后等待通知查看其他人的回应"""


def get_round_dir(repo_root: str, rounds_dir: str, round_num: int) -> str:
    path = os.path.join(repo_root, rounds_dir, f"round_{round_num:03d}")
    os.makedirs(path, exist_ok=True)
    return path


def next_turn_number(round_dir: str, prefix: str) -> int:
    """Find the next available turn number for files matching prefix_NN.md."""
    pattern = os.path.join(round_dir, f"{prefix}_[0-9][0-9].md")
    existing = glob.glob(pattern)
    if not existing:
        return 0
    nums = []
    for path in existing:
        basename = os.path.basename(path)
        num_part = basename.replace(f"{prefix}_", "").replace(".md", "")
        try:
            nums.append(int(num_part))
        except ValueError:
            pass
    return max(nums) + 1 if nums else 0


def write_topic_file(round_dir: str, round_num: int, topic: str,
                     turn: int = 0,
                     previous_responses: dict[str, str] | None = None) -> str:
    """Write the topic.md file for this round, including git instructions."""
    lines = [
        f"# 圆桌讨论 — 第{round_num}轮",
        f"*{datetime.now().strftime('%Y年%m月%d日 %H:%M')}*",
        "",
        "## 议题",
        "",
        topic,
        "",
    ]

    if previous_responses:
        lines.append("## 上一轮各方发言")
        lines.append("")
        for name, text in previous_responses.items():
            lines.append(f"### {name}")
            lines.append("")
            lines.append(text.strip())
            lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("请发表你对以上观点的回应。如果没有补充，可以不提交。")
    else:
        lines.append("这是第一轮。请就这个议题说出你的看法。")

    lines.append("")
    lines.append(_git_instructions(turn))

    filepath = os.path.join(round_dir, "topic.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return filepath


def write_response_file(round_dir: str, name: str, response: str,
                        turn: int | None = None) -> str:
    """Write a response file. If turn is None, auto-increment."""
    if turn is None:
        turn = next_turn_number(round_dir, f"response_{name}")
    filepath = os.path.join(round_dir, f"response_{name}_{turn:02d}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(response)
    return filepath


def read_response_file(round_dir: str, name: str, turn: int = 0) -> str | None:
    """Read a specific turn's response file."""
    filepath = os.path.join(round_dir, f"response_{name}_{turn:02d}.md")
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read().strip()
    return content if content else None


def read_topic_file(round_dir: str) -> str | None:
    filepath = os.path.join(round_dir, "topic.md")
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def collect_all_responses(round_dir: str, participants: list[str],
                          turn: int = 0) -> dict[str, str]:
    """Collect all responses for a specific turn."""
    responses = {}
    for name in participants:
        resp = read_response_file(round_dir, name, turn)
        if resp:
            responses[name] = resp
    return responses


def collect_responses_by_pattern(round_dir: str, name: str) -> list[tuple[int, str]]:
    """Collect all turns of responses from a participant, sorted by turn number."""
    pattern = os.path.join(round_dir, f"response_{name}_[0-9][0-9].md")
    results = []
    for path in sorted(glob.glob(pattern)):
        basename = os.path.basename(path)
        num_part = basename.replace(f"response_{name}_", "").replace(".md", "")
        try:
            turn_num = int(num_part)
        except ValueError:
            continue
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            results.append((turn_num, content))
    return results


def write_summary_file(round_dir: str, round_num: int, topic: str,
                       responses: dict[str, str],
                       turn: int | None = None) -> str:
    """Compile all responses into summary_NN.md for everyone to review."""
    if turn is None:
        turn = next_turn_number(round_dir, "summary")
    lines = [
        f"# 圆桌讨论汇总 — 第{round_num}轮・第{turn}次",
        f"*{datetime.now().strftime('%Y年%m月%d日 %H:%M')}*",
        "",
        f"**议题：** {topic}",
        "",
    ]
    for name, text in responses.items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(text.strip())
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("以上是本轮所有参与者的回应。请查阅。")

    filepath = os.path.join(round_dir, f"summary_{turn:02d}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return filepath
