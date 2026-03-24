"""Git operations for the roundtable file exchange."""

import subprocess
import os


def run_git(repo_root: str, *args) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except Exception as e:
        return False, str(e)


def git_add_commit_push(repo_root: str, files: list[str], message: str) -> bool:
    for f in files:
        ok, out = run_git(repo_root, "add", f)
        if not ok:
            print(f"  git add 失败: {out}")
            return False

    ok, out = run_git(repo_root, "commit", "-m", message)
    if not ok:
        if "nothing to commit" in out:
            print("  无变更需要提交")
            return True
        print(f"  git commit 失败: {out}")
        return False

    ok, out = run_git(repo_root, "push")
    if not ok:
        print(f"  git push 失败: {out}")
        print("  文件已提交到本地，请手动 push")
        return True

    print("  已提交并推送到 GitHub")
    return True


def git_pull(repo_root: str) -> bool:
    ok, out = run_git(repo_root, "pull", "--rebase")
    if not ok:
        ok, out = run_git(repo_root, "pull")
    if not ok:
        print(f"  git pull 失败: {out}")
        return False
    return True
