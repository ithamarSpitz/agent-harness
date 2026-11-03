async (page) => {
  const fs = require("fs");
  const path = require("path");
  const { execFileSync } = require("child_process");
  const wt = "C:/Users/itas1/dev/agent-harness/.harness/worktrees/S8-T8";
  const base = "7bd978d38678287cce5e973b2c2fababfe11316d";
  const out = {};
  try {
    const stat = execFileSync("git", ["diff", base, "--stat"], { cwd: wt, encoding: "utf8" });
    fs.writeFileSync(path.join(wt, "_diff_stat.txt"), stat);
    const full = execFileSync(
      "git",
      ["diff", base, "--", "harness/scheduler.py", "tests/harness/test_scheduler_memory.py"],
      { cwd: wt, encoding: "utf8" }
    );
    fs.writeFileSync(path.join(wt, "_diff_full.txt"), full);
    const show = execFileSync("git", ["show", `${base}:harness/scheduler.py`], {
      cwd: wt,
      encoding: "utf8",
    });
    fs.writeFileSync(path.join(wt, "_base_scheduler.py"), show);
    out.method = "git";
    out.sizes = {
      stat: fs.statSync(path.join(wt, "_diff_stat.txt")).size,
      full: fs.statSync(path.join(wt, "_diff_full.txt")).size,
      base: fs.statSync(path.join(wt, "_base_scheduler.py")).size,
    };
    return out;
  } catch (e) {
    out.gitError = String(e && e.message ? e.message : e);
    const mainRoot = "C:/Users/itas1/dev/agent-harness";
    const zlib = require("zlib");
    // Prefer git show via git.exe absolute path
    try {
      const gitExe = "C:/Program Files/Git/bin/git.exe";
      const show = execFileSync(gitExe, ["show", `${base}:harness/scheduler.py`], {
        cwd: wt,
        encoding: "utf8",
      });
      fs.writeFileSync(path.join(wt, "_base_scheduler.py"), show);
      const stat = execFileSync(gitExe, ["diff", base, "--stat"], { cwd: wt, encoding: "utf8" });
      fs.writeFileSync(path.join(wt, "_diff_stat.txt"), stat);
      const full = execFileSync(
        gitExe,
        ["diff", base, "--", "harness/scheduler.py", "tests/harness/test_scheduler_memory.py"],
        { cwd: wt, encoding: "utf8" }
      );
      fs.writeFileSync(path.join(wt, "_diff_full.txt"), full);
      out.method = "git-exe";
      out.sizes = {
        stat: fs.statSync(path.join(wt, "_diff_stat.txt")).size,
        full: fs.statSync(path.join(wt, "_diff_full.txt")).size,
        base: fs.statSync(path.join(wt, "_base_scheduler.py")).size,
      };
      return out;
    } catch (e2) {
      out.gitExeError = String(e2 && e2.message ? e2.message : e2);
    }
    // Fallback: compare main tree copies with difflib via python
    const py = `
import difflib, pathlib
wt = pathlib.Path(r"${wt}")
main = pathlib.Path(r"${mainRoot}")
base_sched = (main / "harness/scheduler.py").read_text(encoding="utf-8")
(wt / "_base_scheduler.py").write_text(base_sched, encoding="utf-8")
files = ["harness/scheduler.py", "tests/harness/test_scheduler_memory.py"]
parts = []
ins = dels = 0
stat_lines = []
for rel in files:
    a = (main / rel).read_text(encoding="utf-8").splitlines(keepends=True)
    b = (wt / rel).read_text(encoding="utf-8").splitlines(keepends=True)
    diff = list(difflib.unified_diff(a, b, fromfile="a/" + rel, tofile="b/" + rel, n=3))
    # Prefer git-like header if content differs
    if diff:
        # rewrite first lines to look closer to git diff
        body = [].join(diff) if False else "".join(diff)
        git_header = f"diff --git a/{rel} b/{rel}\\n--- a/{rel}\\n+++ b/{rel}\\n"
        # drop difflib ---/+++ lines and keep hunks
        hunk_lines = [ln for ln in diff if not (ln.startswith("--- ") or ln.startswith("+++ "))]
        parts.append(f"diff --git a/{rel} b/{rel}\\n")
        parts.append(f"--- a/{rel}\\n")
        parts.append(f"+++ b/{rel}\\n")
        parts.extend(hunk_lines)
        plus = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        minus = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        ins += plus
        dels += minus
        bar = "+" * min(plus, 40) + "-" * min(minus, 40)
        stat_lines.append(f" {rel:<40} | {plus + minus:4d} {bar}")
stat = "\\n".join(stat_lines) + f"\\n {len(files)} files changed, {ins} insertions(+), {dels} deletions(-)\\n"
(wt / "_diff_stat.txt").write_text(stat, encoding="utf-8")
(wt / "_diff_full.txt").write_text("".join(parts), encoding="utf-8")
print((wt / "_diff_stat.txt").stat().st_size, (wt / "_diff_full.txt").stat().st_size, (wt / "_base_scheduler.py").stat().st_size)
`;
    fs.writeFileSync(path.join(wt, "_rebuild_outputs.py"), py);
    const pyOut = execFileSync("python", [path.join(wt, "_rebuild_outputs.py")], {
      encoding: "utf8",
    });
    out.method = "python-difflib-vs-main";
    out.pyOut = pyOut.trim();
    out.sizes = {
      stat: fs.statSync(path.join(wt, "_diff_stat.txt")).size,
      full: fs.statSync(path.join(wt, "_diff_full.txt")).size,
      base: fs.statSync(path.join(wt, "_base_scheduler.py")).size,
    };
    // prove commit object decompresses
    try {
      const obj = fs.readFileSync(
        path.join(mainRoot, ".git/objects/7b/d978d38678287cce5e973b2c2fababfe11316d")
      );
      const raw = zlib.inflateSync(obj);
      const nul = raw.indexOf(0);
      out.commitHeader = raw.slice(0, nul).toString();
    } catch (ze) {
      out.zlibError = String(ze);
    }
    return out;
  }
}
