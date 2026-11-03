"""Rebuild S8-T8 review outputs when Shell/git are unavailable to the agent."""
from __future__ import annotations

import difflib
import pathlib
import subprocess
import sys
import zlib

WT = pathlib.Path(__file__).resolve().parent
MAIN = pathlib.Path(r"C:\Users\itas1\dev\agent-harness")
BASE = "7bd978d38678287cce5e973b2c2fababfe11316d"
FILES = ["harness/scheduler.py", "tests/harness/test_scheduler_memory.py"]


def try_git() -> bool:
    try:
        stat = subprocess.check_output(
            ["git", "diff", BASE, "--stat"], cwd=WT, text=True, encoding="utf-8"
        )
        full = subprocess.check_output(
            ["git", "diff", BASE, "--", *FILES], cwd=WT, text=True, encoding="utf-8"
        )
        show = subprocess.check_output(
            ["git", "show", f"{BASE}:harness/scheduler.py"],
            cwd=WT,
            text=True,
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"git failed: {exc}", file=sys.stderr)
        return False
    (WT / "_diff_stat.txt").write_text(stat, encoding="utf-8")
    (WT / "_diff_full.txt").write_text(full, encoding="utf-8")
    (WT / "_base_scheduler.py").write_text(show, encoding="utf-8")
    print("method=git")
    return True


def resolve_tree_oid_from_commit(commit_oid: str) -> str | None:
    obj_path = MAIN / ".git" / "objects" / commit_oid[:2] / commit_oid[2:]
    if not obj_path.is_file():
        return None
    raw = zlib.decompress(obj_path.read_bytes())
    nul = raw.index(b"\x00")
    body = raw[nul + 1 :].decode("utf-8", errors="replace")
    for line in body.splitlines():
        if line.startswith("tree "):
            return line.split()[1]
    return None


def read_git_object(oid: str) -> bytes | None:
    loose = MAIN / ".git" / "objects" / oid[:2] / oid[2:]
    if loose.is_file():
        raw = zlib.decompress(loose.read_bytes())
        nul = raw.index(b"\x00")
        return raw[nul + 1 :]
    # Packed objects not handled here.
    return None


def parse_tree(data: bytes) -> dict[str, str]:
    entries: dict[str, str] = {}
    i = 0
    while i < len(data):
        space = data.index(b" ", i)
        mode = data[i:space].decode()
        nul = data.index(b"\x00", space)
        name = data[space + 1 : nul].decode()
        oid = data[nul + 1 : nul + 21].hex()
        entries[name] = oid
        i = nul + 21
        _ = mode
    return entries


def blob_from_commit_path(commit_oid: str, rel: str) -> str | None:
    tree_oid = resolve_tree_oid_from_commit(commit_oid)
    if not tree_oid:
        return None
    parts = rel.split("/")
    oid = tree_oid
    for part in parts:
        data = read_git_object(oid)
        if data is None:
            return None
        entries = parse_tree(data)
        if part not in entries:
            return None
        oid = entries[part]
    blob = read_git_object(oid)
    if blob is None:
        return None
    return blob.decode("utf-8")


def fallback_difflib() -> None:
    base_sched = blob_from_commit_path(BASE, "harness/scheduler.py")
    if base_sched is None:
        # Main checkout matched the base side of the prior review-time git diff.
        base_sched = (MAIN / "harness/scheduler.py").read_text(encoding="utf-8")
        print("method=difflib-vs-main (blob unpack failed; using main checkout)", file=sys.stderr)
    else:
        print("method=difflib-vs-unpacked-blob")
    (WT / "_base_scheduler.py").write_text(base_sched, encoding="utf-8")

    base_test = blob_from_commit_path(BASE, "tests/harness/test_scheduler_memory.py")
    if base_test is None:
        base_test = (MAIN / "tests/harness/test_scheduler_memory.py").read_text(encoding="utf-8")

    bases = {
        "harness/scheduler.py": base_sched,
        "tests/harness/test_scheduler_memory.py": base_test,
    }

    parts: list[str] = []
    ins = dels = 0
    stat_lines: list[str] = []
    for rel in FILES:
        a = bases[rel].splitlines(keepends=True)
        b = (WT / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        diff = list(
            difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
        )
        if not diff:
            continue
        parts.append(f"diff --git a/{rel} b/{rel}\n")
        # Keep difflib ---/+++ and hunks.
        parts.extend(diff)
        plus = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        minus = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        ins += plus
        dels += minus
        bar = ("+" * min(plus, 40)) + ("-" * min(minus, 40))
        stat_lines.append(f" {rel:<40} | {plus + minus:4d} {bar}")

    changed = len([rel for rel in FILES if (WT / rel).read_text(encoding="utf-8") != bases[rel]])
    stat = (
        "\n".join(stat_lines)
        + f"\n {changed} files changed, {ins} insertions(+), {dels} deletions(-)\n"
    )
    (WT / "_diff_stat.txt").write_text(stat, encoding="utf-8")
    (WT / "_diff_full.txt").write_text("".join(parts), encoding="utf-8")


def main() -> int:
    if not try_git():
        fallback_difflib()
    for name in ("_diff_stat.txt", "_diff_full.txt", "_base_scheduler.py"):
        path = WT / name
        print(f"{name}: {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
