#!/usr/bin/env python3
"""cloud-oneoff — разові хмарні виконавці (routine run_once_at): промпт, сторож гілки, перевірка перед
злиттям, конфлікт Changelog, закриття задачі. Лише stdlib; macOS/Linux; у Git Bash на Windows
`idle` чесно повертає UNKNOWN.

Підкоманди:
  body      — JSON тіла RemoteTrigger create (промпт виконавця нової задачі або ітерації N >= 2)
  watch     — сторож: звіт уже на гілці при старті → ALREADY REPORTED; інакше чекає нового коміту з
              `status: reported` (і за потреби розділу ітерації); збої мережі — повтор, BLIND лише N разів поспіль
  idle      — чи не працює конвеєр із робочого дерева (за cwd і командним рядком). 0 IDLE, 1 BUSY, 3 UNKNOWN
  changelog — розв'язати ОДИН конфлікт злиття в Changelog-файлі: обидва записи, гілка зверху
  close     — останній вердикт VERIFIED → status: done, git add, git mv у tasks/done/, [x] у черзі, коміт лише цих шляхів

Приклади — у SKILL.md поруч.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r


def read_text(p):
    return Path(p).read_bytes().decode("utf-8")


def write_text(p, s):
    # newline="" semantics: keep "\n" as written (no CRLF translation on Windows)
    Path(p).write_bytes(s.encode("utf-8"))


# ---------------------------------------------------------------- body
DEFAULT_TEST_CMD = "python -m pytest -q -p no:cacheprovider tests/unit"
DEFAULT_SETUP = "Python >= 3.12 venv, `pip install -r requirements.txt` plus test dependencies."

PROMPT_NEW = """You are a cloud executor (L2 fallback) for the {project} project{about}. The owner approved task {id}; the Architect runs it now as a one-off cloud run (routine run_once_at). Execute ONLY task {id}, then stop. Write commit messages and reports in {lang}.

## Step 1: claim
1. `git fetch origin --prune`. If branch `{branch}` already exists on origin, print `ALREADY TAKEN` and STOP, changing nothing.
2. Read `git show origin/{main}:{task}`. If its status is not `pending`, print why and STOP. If it has a precondition section (`# ПЕРЕДУМОВА` / `# PRECONDITION`) that is not met in `origin/{main}`, print `PRECONDITION NOT MET` and STOP without creating a branch.
3. `git checkout -b {branch} origin/{main}`; change `status: pending` to `status: in_progress` in `{task}`; commit (message in {lang}) `[L2 fallback] {id}: taken by the cloud executor, status: in_progress`, ending with the line `{coauthor}`; `git push -u origin {branch}`.
"""

PROMPT_ITER = """You are a cloud executor (L2 fallback) for the {project} project{about}. The owner approved task {id}. The Architect returned the previous iteration; you do ITERATION {iteration} on the EXISTING branch `{branch}` as a one-off cloud run. Execute only this, then stop. Write commit messages and reports in {lang}.

## Step 1: take the branch
1. `git fetch origin --prune`; `git checkout -B {branch} origin/{branch}`.
2. The LAST section of `{task}` must be the Architect's verdict returning the task for iteration {iteration}, and the status must be `pending`. If not, print why and STOP. Set `status: in_progress`, commit (message in {lang}) `[L2 fallback] {id}: iteration {iteration} taken, status: in_progress`, ending with `{coauthor}`; push.
3. `git merge origin/{main}`. Changelog-only conflicts (entries at the top): keep all entries. Any conflict in code: `git merge --abort`, do NOT resolve it yourself, describe it in the report and continue on the branch without the merge.
"""

PROMPT_BODY = """
## Step 2: execute
Read `{task}` in full{iter_note}. Its SSOT Context, goal, steps, constraints and Definition of Done bind you. Before designing, read the relevant SSOT sections of the project (Context / Changelog), then the code the task names, reading each file from the top. Line numbers may have shifted: find them again by content. Every claim about existing behaviour in your report must cite file:line.

Environment: {setup}

Hard rules:
- Work ONLY on `{branch}`. NEVER push to or merge into `{main}`. Never touch other branches.
- Paths you must NOT modify (other executors or L1 work there): {forbid}. Respect the task file's constraints section as well. If the task cannot be done without a forbidden path, stop and report it.
- Timing and platform tests: thresholds relative to a baseline measured in the same run, never absolute; OS-dependent checks use `skipif` with a reason. The Architect's machine may have a case-insensitive filesystem (macOS/Windows): no test fixtures with paths that differ only in letter case. The suite must pass there and on Linux.
- No production data here by design (gitignored data/config state is absent). Do not create or imitate it; tests must never write into data or config directories. Never fake numbers: for items that need production data or hardware, write exact read-only commands for the Architect.
- No paid API calls, no network access in tests, no API keys. Never edit `.env`. Never restart or deploy anything. Never run pipelines or scrapers against the network.
- BEFORE changes, record the exact failing-test list of `{test_cmd}` as the baseline; after changes the set must be identical or smaller. NEVER edit or skip existing tests to hide failures, unless the task file explicitly allows a named edit.
- Run the mutations the task demands; each must turn a test red. Paste results; state plainly if any survives.
- Commits start with `[L2 fallback] {id}:` and end with the line `{coauthor}`. `git add` by name only. Update the SSOT files the task requires{ssot_iter}.
- No changes outside the task's scope; describe them in the report instead. If the task is ambiguous, contradicts the code, or needs an owner decision, do the safe part, describe the blocker, and still finish with `status: reported`.
{extra}
## Step 3: report and finish
Append {report_head} at the END of `{task}` (the file may only be appended to): what was done, verification, mutation check, errors & obstacles, SSOT. State that the task ran as a cloud one-off on branch `{branch}`; baseline and after failing tests with counts; the language/runtime version; local commands for the Architect. Write no verdict words. Set `status: reported`, commit, push the branch, then STOP.
"""


def cmd_body(a):
    if a.iteration is not None and a.iteration < 2:
        sys.exit("--iteration must be >= 2 (iteration 1 is the plain run)")
    if not a.forbid.strip():
        sys.exit("--forbid is mandatory (protocol): list busy paths or pass --forbid none explicitly")
    branch = a.branch or f"fallback/{a.id.lower()}"
    task = a.task or f".agents/tasks/{a.id}.md"
    forbid = ("none — the Architect declares no busy zones for this run" if a.forbid.strip().lower() == "none"
              else a.forbid)
    fields = dict(
        project=a.project, about=f": {a.about}" if a.about else "", id=a.id, branch=branch, main=a.main,
        task=task, lang=a.lang, coauthor=a.coauthor, iteration=a.iteration or 1, forbid=forbid,
        test_cmd=a.test_cmd, setup=a.setup,
        iter_note=(", including your previous report and the Architect's latest verdict; the verdict's points are binding"
                   if a.iteration else ""),
        ssot_iter=(" and extend (do not duplicate) the task's existing Changelog entry" if a.iteration else ""),
        report_head=(f"a section `## Ітерація {a.iteration}` (or `## Iteration {a.iteration}`)" if a.iteration
                     else "the report under the template's Report sections"),
        extra=("\n## Extra binding instructions from the Architect\n" + a.extra.strip() + "\n") if a.extra else "",
    )
    prompt = (PROMPT_ITER if a.iteration else PROMPT_NEW).format(**fields) + PROMPT_BODY.format(**fields)
    if a.test_cmd == DEFAULT_TEST_CMD or a.setup == DEFAULT_SETUP:
        print("WARNING: --test-cmd/--setup defaults are Python/pytest — pass the project's own for non-Python repos",
              file=sys.stderr)
    if a.run_at:
        try:
            t = dt.datetime.strptime(a.run_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            sys.exit("--run-at must be UTC like 2026-09-30T06:00:00Z")
        if t <= dt.datetime.now(dt.timezone.utc):
            sys.exit("--run-at is in the past")
        run_at = a.run_at
    else:
        run_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=a.in_minutes)).strftime("%Y-%m-%dT%H:%M:00Z")
    body = {
        "name": f"{a.id} {'iteration ' + str(a.iteration) + ' ' if a.iteration else ''}one-off (L2 fallback)",
        "run_once_at": run_at,
        "job_config": {"ccr": {
            "environment_id": a.env_id,
            "session_context": {"allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
                                "model": a.model,
                                "sources": [{"git_repository": {"url": a.repo_url}}]},
            "events": [{"data": {"uuid": str(uuid.uuid4()), "session_id": "", "type": "user",
                                 "parent_tool_use_id": None,
                                 "message": {"role": "user", "content": prompt}}}]}},
    }
    print(prompt if a.prompt_only else json.dumps(body, ensure_ascii=False))


# ---------------------------------------------------------------- watch
class Blind(Exception):
    """git did not see origin (DNS, network, auth, a racing push): transient inside the loop, fatal only in a row."""


def hhmm():
    return dt.datetime.now().strftime('%H:%M')


def remote_sha(repo, branch):
    """sha of refs/heads/<branch> on origin via ls-remote; None if absent; raises Blind if ls-remote fails."""
    r = run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=repo, check=False)
    if r.returncode != 0:
        raise Blind(f"git ls-remote failed: {r.stderr.strip()}")
    line = r.stdout.strip().splitlines()
    return line[0].split()[0] if line else None


def fetch_exact(repo, branch, sha):
    """Fetch the branch and read it by sha (not by the shared FETCH_HEAD). Raises Blind if the fetch cannot see it."""
    run(["git", "update-ref", "-d", f"refs/oneoff-watch/{branch}"], cwd=repo, check=False)  # no stale ref may pass the check
    r = run(["git", "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/oneoff-watch/{branch}"], cwd=repo, check=False)
    got = run(["git", "rev-parse", "-q", "--verify", f"refs/oneoff-watch/{branch}"], cwd=repo, check=False).stdout.strip()
    if r.returncode != 0 or got != sha:
        raise Blind(f"fetch of {branch} did not deliver {sha[:10]} (got {got[:10] or 'nothing'}): {r.stderr.strip()}")


def is_reported(repo, sha, a):
    """The task file at <sha> has `status: reported` in its frontmatter (and the --require text, if given)."""
    s = run(["git", "show", f"{sha}:{a.task}"], cwd=repo, check=False)
    fm = s.stdout.split("\n---", 2)[0] if s.stdout.startswith("---") else s.stdout[:2000]
    return (s.returncode == 0 and bool(re.search(r"^status:\s*reported\s*$", fm, re.M))
            and (not a.require or a.require in s.stdout))


def cmd_watch(a):
    repo = a.repo
    probe = a.self_check or a.main

    def start_state():
        psha = remote_sha(repo, probe)
        if not psha:
            print(f"SELF-CHECK FAILED: origin has no '{probe}' — the watcher would be blind")
            sys.exit(2)
        fetch_exact(repo, probe, psha)  # the same mechanism the loop uses must see an existing branch
        start = remote_sha(repo, a.branch)
        if start:
            fetch_exact(repo, a.branch, start)
        return psha, start, bool(start) and is_reported(repo, start, a)

    a.start_retries = max(1, a.start_retries)
    for attempt in range(1, a.start_retries + 1):
        try:
            psha, start, done = start_state()
            break
        except Blind as e:
            if attempt >= a.start_retries:
                cleanup_refs(repo, probe, a.branch)
                print(f"SELF-CHECK FAILED after {attempt} attempt(s) — the watcher would be blind: {e}")
                sys.exit(2)
            print(f"{hhmm()} self-check attempt {attempt}/{a.start_retries} failed, retrying: {e}", flush=True)
            time.sleep(min(a.interval, 15))
    if done:  # the report was on the branch before the watcher started (урок DDL 30.09: звіт ~2 год лежав непоміченим)
        log = run(["git", "log", "--oneline", "-3", start], cwd=repo, check=False).stdout
        print(f"ALREADY REPORTED {start[:10]} {a.branch} (on the branch at start, {hhmm()})\n{log}")
        cleanup_refs(repo, probe, a.branch)
        return
    print(f"self-check ok ({probe} {psha[:10]}); waiting for {a.branch} "
          f"(now {start[:10] + ', not reported' if start else 'absent'}): new commit with status reported"
          + (f" + '{a.require}'" if a.require else ""), flush=True)
    last, fails = start, 0
    for n in range(a.max):
        try:
            sha = remote_sha(repo, a.branch)
            if sha and sha != last:
                fetch_exact(repo, a.branch, sha)
                if is_reported(repo, sha, a):
                    log = run(["git", "log", "--oneline", "-3", sha], cwd=repo, check=False).stdout
                    print(f"REPORTED {hhmm()} {a.branch} {sha[:10]}\n{log}")
                    cleanup_refs(repo, probe, a.branch)
                    return
                print(f"{hhmm()} {a.branch} moved to {sha[:10]} (not reported yet)", flush=True)
                last = sha  # only after a successful read: a failed fetch is read again on the next poll
            fails = 0
        except Blind as e:
            fails += 1
            if fails >= a.max_failures:
                cleanup_refs(repo, probe, a.branch)
                print(f"BLIND: {fails} consecutive failures, last: {e}")
                sys.exit(2)
            print(f"{hhmm()} transient failure {fails}/{a.max_failures}, retrying on the next poll: {e}", flush=True)
        if a.heartbeat and n and n % a.heartbeat == 0:
            print(f"{hhmm()} still waiting ({a.branch} {last[:10] if last else 'absent'})", flush=True)
        time.sleep(a.interval)
    cleanup_refs(repo, probe, a.branch)
    print("TIMEOUT")
    sys.exit(1)


def cleanup_refs(repo, *branches):
    for b in branches:
        run(["git", "update-ref", "-d", f"refs/oneoff-watch/{b}"], cwd=repo, check=False)


# ---------------------------------------------------------------- idle
def cmd_idle(a):
    repo = Path(a.repo).resolve()
    repo_names = {str(repo), str(Path(a.repo).absolute()).rstrip("/")}
    busy, unknown = [], []
    for lock in a.lock or []:
        if Path(lock).exists():
            busy.append(f"lock {lock}")
    if os.name == "nt" or os.environ.get("MSYSTEM"):
        unknown.append("Windows / Git Bash: process cwd cannot be checked — verify manually (Get-Process)")
    else:
        pat = re.compile(a.proc)
        ps = run(["ps", "-eo", "pid=,args="], check=False)
        if ps.returncode != 0 or not ps.stdout.strip():
            unknown.append(f"ps failed: {ps.stderr.strip()[:200]}")
        me = str(os.getpid())
        for line in ps.stdout.splitlines():
            line = line.strip()
            if not line or not pat.search(line):
                continue
            pid, _, cmdline = line.partition(" ")
            if pid == me or "oneoff.py" in cmdline:
                continue
            if any(n in cmdline for n in repo_names):
                busy.append(f"pid {pid} (repo path in command line): {cmdline[:120]}")
                continue
            lo = run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], check=False)
            cwd = next((l[1:] for l in lo.stdout.splitlines() if l.startswith("n")), "")
            if not cwd:
                if run(["ps", "-p", pid], check=False).returncode != 0:
                    continue  # the process has already exited (ps -p, not kill -0: EPERM on others' processes)
                unknown.append(f"pid {pid}: cwd unknown (lsof: {lo.stderr.strip()[:80]}): {cmdline[:100]}")
                continue
            c = Path(cwd).resolve()
            if c == repo or repo in c.parents:
                busy.append(f"pid {pid} (cwd {cwd}): {cmdline[:120]}")
            else:
                print(f"ignored pid {pid}: cwd {cwd} (outside the repo — e.g. a test in a temp copy)")
    if busy:
        print("BUSY:\n  " + "\n  ".join(busy))
        sys.exit(1)
    if unknown:
        print("UNKNOWN (do not merge until checked):\n  " + "\n  ".join(unknown))
        sys.exit(3)
    print("IDLE")


# ---------------------------------------------------------------- changelog
def cmd_changelog(a):
    raw = read_text(a.file)
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if l.startswith("<<<<<<< ")]
    if len(starts) != 1:
        sys.exit(f"expected exactly one conflict block, found {len(starts)} — resolve by hand")
    s = starts[0]
    try:
        m = next(i for i in range(s, len(lines)) if lines[i] == "=======")
        e = next(i for i in range(m, len(lines)) if lines[i].startswith(">>>>>>> "))
    except StopIteration:
        sys.exit("malformed conflict block")
    base = next((i for i in range(s, m) if lines[i].startswith("||||||| ")), None)  # diff3 / zdiff3 style
    ours_end = base if base is not None else m

    def strip(x):
        while x and x[-1] == "":
            x = x[:-1]
        return x
    ours, theirs = strip(lines[s + 1:ours_end]), strip(lines[m + 1:e])
    new = (theirs + [""] + ours) if a.branch_first else (ours + [""] + theirs)
    rest = lines[e + 1:]
    sep = [] if rest[:1] == [""] else [""]
    out = "\n".join(lines[:s] + new + sep + rest)
    if re.search(r"^(<<<<<<<|=======|>>>>>>>|\|\|\|\|\|\|\|)( |$)", out, re.M):
        sys.exit("conflict markers would remain — resolve by hand")
    write_text(a.file, out.replace("\n", "\r\n") if crlf else out)
    print(f"resolved: kept both entries ({'branch' if a.branch_first else 'HEAD'} first)"
          + (" [diff3 base dropped]" if base is not None else "")
          + (f"; note: conflict starts at line {s + 1}, not at the top" if s > 5 else "")
          + f"\nnext: git add {a.file}")


# ---------------------------------------------------------------- close
VERDICT_HEAD = re.compile(r"^#{1,2}[ \t]*Verdict\b.*$", re.M)
LATER_SECTION = re.compile(r"^#{1,2}[ \t]*(Ітерація|Iteration|Report)\b", re.M)
NEGATIVE = re.compile(r"REJECTED|доробк|rework|не[ \t]+VERIFIED|NOT[ \t]+VERIFIED|UNVERIFIED", re.I)


def last_verdict_ok(text):
    heads = list(VERDICT_HEAD.finditer(text))
    if not heads:
        return False, "no '# Verdict' heading"
    h = heads[-1]
    after = text[h.end():]
    if LATER_SECTION.search(after):
        return False, ("there is a later iteration/report section after the last '# Verdict' heading "
                       "(the verdict heading must contain the word 'Verdict', e.g. '# Verdict — VERIFIED (…)')")
    head = h.group(0)
    first = next((l for l in after.split("\n") if l.strip()), "")
    if NEGATIVE.search(head):
        return False, f"last verdict heading is negative: {head.strip()}"
    if re.search(r"\bVERIFIED\b", head) or (re.search(r"\bVERIFIED\b", first) and not NEGATIVE.search(first)):
        return True, head.strip()
    return False, f"last verdict is not VERIFIED: {head.strip()} / {first.strip()[:80]}"


def has_push_trigger(yml):
    """True for `on: push`, `on: [push, …]`, `"on": push` and the block form `on:` / `  push:`."""
    lines = [re.sub(r"\s+#.*$", "", l) for l in yml.replace("\r\n", "\n").split("\n")]
    for i, l in enumerate(lines):
        m = re.match(r"^(\s*)['\"]?on['\"]?\s*:(.*)$", l)
        if not m:
            continue
        if re.search(r"\bpush\b", m.group(2)):
            return True
        ind = len(m.group(1))
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= ind:
                break
            if re.match(r"^\s*-?\s*['\"]?push['\"]?\s*(:|$)", nxt):
                return True
    return False


def cmd_close(a):
    repo = Path(a.repo)
    merge_head = run(["git", "rev-parse", "--git-path", "MERGE_HEAD"], cwd=repo).stdout.strip()
    if (repo / merge_head).exists():  # --git-path is relative to cwd (= repo) or absolute
        sys.exit("a merge is in progress (MERGE_HEAD) — commit the merge first")
    head_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()
    if head_branch != a.main:
        sys.exit(f"HEAD is '{head_branch}', not '{a.main}' — close on the main branch")
    rel_task = f"{a.tasks_dir}/{a.id}.md"
    rel_done = f"{a.tasks_dir}/done/{a.id}.md"
    task = repo / rel_task
    if not task.exists():
        sys.exit(f"no {task}")
    text = read_text(task)
    ok, why = last_verdict_ok(text)
    if not ok:
        sys.exit(f"refused: {why}")
    fm = text.split("\n---", 2)[0] if text.startswith("---") else ""
    if re.search(r"^hil_required:\s*true\b", fm, re.M | re.I) and not a.hil_pass:
        sys.exit("refused: hil_required: true — close only after the owner's written HIL PASS (pass --hil-pass)")
    staged = [l for l in run(["git", "diff", "--cached", "--name-only"], cwd=repo).stdout.splitlines() if l]
    own = {rel_task, rel_done, a.queue}
    foreign = [p for p in staged if p not in own]
    if foreign:
        sys.exit("refused: foreign staged changes would enter the commit: " + ", ".join(foreign))
    (repo / a.tasks_dir / "done").mkdir(parents=True, exist_ok=True)
    write_text(task, re.sub(r"^status:\s*\S+\s*$", "status: done", text, count=1, flags=re.M))
    run(["git", "add", "--", rel_task], cwd=repo)
    run(["git", "mv", "--", rel_task, rel_done], cwd=repo)
    paths = [rel_task, rel_done]
    q = repo / a.queue
    if a.queue and q.exists():
        qt = read_text(q)
        nq, n = re.subn(rf"^- \[ \] {re.escape(a.id)} — ", f"- [x] {a.id} — (злито {dt.date.today():%d.%m}) ",
                        qt, flags=re.M)
        if n:
            write_text(q, nq)
            run(["git", "add", "--", a.queue], cwd=repo)
            paths.append(a.queue)
        else:
            print(f"queue: no '- [ ] {a.id}' line in {a.queue} (not queued, or already marked)")
    msg = f"{a.id}: VERIFIED — злито ({a.merge}), done\n\n{a.coauthor}"
    run(["git", "commit", "-q", "-m", msg, "--", *paths], cwd=repo)
    print(f"closed {a.id} ({why}): {', '.join(paths[1:])}")
    if a.push:
        wf = repo / ".github" / "workflows"
        if wf.is_dir() and any(has_push_trigger(read_text(f)) for f in wf.glob("*.y*ml")) and not a.push_is_not_deploy:
            sys.exit("NOT pushed: .github/workflows has a push trigger — push may be a deploy. "
                     "Check paths-ignore, then re-run `git push` yourself or pass --push-is-not-deploy.")
        run(["git", "push", "-q", "origin", a.main], cwd=repo)
        print("pushed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    coauthor = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"

    b = sp.add_parser("body", help="RemoteTrigger create body (JSON) or --prompt-only")
    b.add_argument("--id", required=True)
    b.add_argument("--project", required=True)
    b.add_argument("--about", default="", help="one-line description of the project for the executor")
    b.add_argument("--repo-url", required=True)
    b.add_argument("--env-id", required=True, help="from an existing routine: RemoteTrigger get → job_config.ccr.environment_id")
    b.add_argument("--model", default="claude-opus-5-5")
    b.add_argument("--coauthor", default=coauthor, help="attribution line for commits")
    b.add_argument("--main", default="main")
    b.add_argument("--branch")
    b.add_argument("--task")
    b.add_argument("--lang", default="Ukrainian")
    b.add_argument("--iteration", type=int, help="N >= 2: rework on the existing branch")
    b.add_argument("--forbid", required=True, help="busy paths the executor must not modify, or 'none' (mandatory by protocol)")
    b.add_argument("--test-cmd", default=DEFAULT_TEST_CMD)
    b.add_argument("--setup", default=DEFAULT_SETUP)
    b.add_argument("--extra", help="extra binding instructions (inserted before Step 3)")
    b.add_argument("--run-at", help="UTC like 2026-09-30T06:00:00Z")
    b.add_argument("--in-minutes", type=int, default=2)
    b.add_argument("--prompt-only", action="store_true")
    b.set_defaults(fn=cmd_body)

    w = sp.add_parser("watch", help="wait for a new commit with status: reported on the branch (run in background)")
    w.add_argument("--repo", default=".")
    w.add_argument("--branch", required=True)
    w.add_argument("--task", required=True, help="path of the task file inside the repo")
    w.add_argument("--require", help="text that must be present too, e.g. '## Ітерація 2'")
    w.add_argument("--main", default="main")
    w.add_argument("--self-check", help="branch that must already exist on origin (default: main)")
    w.add_argument("--interval", type=int, default=120)
    w.add_argument("--max", type=int, default=180)
    w.add_argument("--heartbeat", type=int, default=0, help="print 'still waiting' every N polls (0 = off)")
    w.add_argument("--max-failures", type=int, default=10,
                   help="exit 2 BLIND only after N consecutive ls-remote/fetch failures (transient DNS etc. are retried)")
    w.add_argument("--start-retries", type=int, default=3, help="attempts of the start self-check before SELF-CHECK FAILED")
    w.set_defaults(fn=cmd_watch)

    i = sp.add_parser("idle", help="exit 0 IDLE / 1 BUSY / 3 UNKNOWN — pipeline running from this working tree?")
    i.add_argument("--repo", default=".")
    i.add_argument("--proc", required=True, help="regex of pipeline process command lines")
    i.add_argument("--lock", action="append", help="lock path that means 'busy' (repeatable)")
    i.set_defaults(fn=cmd_idle)

    c = sp.add_parser("changelog", help="resolve exactly one merge conflict block keeping both entries")
    c.add_argument("file")
    c.add_argument("--head-first", dest="branch_first", action="store_false",
                   help="keep HEAD's entry on top (default: the merged branch's entry on top)")
    c.set_defaults(fn=cmd_changelog, branch_first=True)

    k = sp.add_parser("close", help="last verdict VERIFIED → done, move to tasks/done, [x] in queue, commit only these paths")
    k.add_argument("--repo", default=".")
    k.add_argument("--id", required=True)
    k.add_argument("--merge", required=True, help="merge commit hash for the message")
    k.add_argument("--tasks-dir", default=".agents/tasks")
    k.add_argument("--queue", default=".agents/cloud_queue.md")
    k.add_argument("--main", default="main")
    k.add_argument("--hil-pass", action="store_true", help="the owner's written HIL PASS exists (required for hil_required: true)")
    k.add_argument("--coauthor", default=coauthor)
    k.add_argument("--push", action="store_true")
    k.add_argument("--push-is-not-deploy", action="store_true",
                   help="you checked .github/workflows paths-ignore: this push does not deploy")
    k.set_defaults(fn=cmd_close)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
