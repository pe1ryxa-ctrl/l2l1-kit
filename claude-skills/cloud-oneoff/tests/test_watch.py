"""Тести сторожа `oneoff.py watch`: тимчасовий bare-репозиторій як origin, клони, без мережі.

Запуск: python -m pytest -q -p no:cacheprovider claude-skills/cloud-oneoff/tests
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import oneoff  # noqa: E402

BRANCH = "fallback/t-001"
TASK = ".agents/tasks/T-001.md"


def git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def task_text(status, extra=""):
    return f"---\nid: T-001\nstatus: {status}\n---\n\n# Мета\nщось\n{extra}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """origin (bare) + work (виконавець пушить) + watch (клон, у якому працює сторож)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.invalid")
    origin, work, watch = tmp_path / "origin.git", tmp_path / "work", tmp_path / "watch"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
    commit(work, task_text("pending"), "task")
    git("push", "-q", "origin", "main", cwd=work)
    git("clone", "-q", str(origin), str(watch), cwd=tmp_path)
    return {"work": work, "watch": watch}


def commit(work, text, msg, branch=None, push=False):
    if branch:
        git("checkout", "-q", "-B", branch, cwd=work)
    p = work / TASK
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    git("add", TASK, cwd=work)
    git("commit", "-q", "-m", msg, cwd=work)
    if push:
        git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch or 'main'}", cwd=work)


def run_watch(monkeypatch, capsys, env, *extra, on_sleep=None):
    """Запускає watch у процесі; повертає (exit code, stdout, кількість sleep-викликів)."""
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if on_sleep:
            on_sleep(len(sleeps))

    monkeypatch.setattr(oneoff.time, "sleep", fake_sleep)
    argv = ["oneoff.py", "watch", "--repo", str(env["watch"]), "--branch", BRANCH, "--task", TASK,
            "--interval", "0", *extra]
    monkeypatch.setattr(sys, "argv", argv)
    code = 0
    try:
        oneoff.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    out = capsys.readouterr().out
    return code, out, len(sleeps)


def no_watch_refs(env):
    return git("for-each-ref", "refs/oneoff-watch", cwd=env["watch"]) == ""


def test_already_reported_at_start_exits_immediately(env, monkeypatch, capsys):
    commit(env["work"], task_text("reported", "# Report\nok\n"), "report", branch=BRANCH, push=True)
    code, out, sleeps = run_watch(monkeypatch, capsys, env, "--max", "5")
    assert code == 0
    assert out.startswith("ALREADY REPORTED ")
    assert "REPORTED" in out and "TIMEOUT" not in out
    assert sleeps == 0
    assert no_watch_refs(env)


def test_pending_then_reported_commit_is_detected(env, monkeypatch, capsys):
    commit(env["work"], task_text("in_progress"), "taken", branch=BRANCH, push=True)

    def push_report(n):
        if n == 1:
            commit(env["work"], task_text("reported", "# Report\nok\n"), "report", branch=BRANCH, push=True)

    code, out, sleeps = run_watch(monkeypatch, capsys, env, "--max", "5", on_sleep=push_report)
    assert code == 0
    assert "ALREADY REPORTED" not in out
    assert "\nREPORTED " in out
    assert sleeps == 1
    assert no_watch_refs(env)


def test_absent_branch_then_reported_is_detected(env, monkeypatch, capsys):
    def push_report(n):
        if n == 2:
            commit(env["work"], task_text("reported"), "report", branch=BRANCH, push=True)

    code, out, _ = run_watch(monkeypatch, capsys, env, "--max", "5", on_sleep=push_report)
    assert code == 0
    assert "absent" in out and "\nREPORTED " in out


def test_require_missing_does_not_fire(env, monkeypatch, capsys):
    # старий звіт ітерації 1 на гілці + новий коміт без розділу ітерації 2 — не спрацьовує
    commit(env["work"], task_text("reported", "# Report\nіт.1\n"), "report 1", branch=BRANCH, push=True)

    def push_other(n):
        if n == 1:
            commit(env["work"], task_text("reported", "# Report\nіт.1\nще\n"), "noise", branch=BRANCH, push=True)

    code, out, _ = run_watch(monkeypatch, capsys, env, "--max", "3", "--require", "## Ітерація 2",
                             on_sleep=push_other)
    assert code == 1
    assert "REPORTED" not in out.replace("not reported", "")
    assert "moved to" in out and out.rstrip().endswith("TIMEOUT")
    assert no_watch_refs(env)


def test_require_present_fires(env, monkeypatch, capsys):
    commit(env["work"], task_text("pending", "# Report\nіт.1\n# Verdict\nдоробка\n"), "verdict", branch=BRANCH,
           push=True)

    def push_iter2(n):
        if n == 1:
            commit(env["work"], task_text("reported", "# Report\nіт.1\n# Verdict\nдоробка\n## Ітерація 2\nok\n"),
                   "iter 2", branch=BRANCH, push=True)

    code, out, _ = run_watch(monkeypatch, capsys, env, "--max", "3", "--require", "## Ітерація 2",
                             on_sleep=push_iter2)
    assert code == 0 and "\nREPORTED " in out


def flaky_runner(monkeypatch, state):
    """Заглушка git-раннера: поки state['fail'] > 0, ls-remote падає як при збої DNS."""
    real = oneoff.run

    def fake(cmd, cwd=None, check=True):
        if cmd[:2] == ["git", "ls-remote"] and state["fail"] > 0:
            state["fail"] -= 1
            state["failed"] += 1
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: unable to access: Could not resolve host: github.com")
        return real(cmd, cwd=cwd, check=check)

    monkeypatch.setattr(oneoff, "run", fake)


def test_transient_failures_are_retried(env, monkeypatch, capsys):
    commit(env["work"], task_text("in_progress"), "taken", branch=BRANCH, push=True)
    state = {"fail": 0, "failed": 0}
    flaky_runner(monkeypatch, state)

    def outage(n):
        if n == 1:  # після першого опитування: два збої поспіль, тим часом приходить звіт
            state["fail"] = 2
            commit(env["work"], task_text("reported"), "report", branch=BRANCH, push=True)

    code, out, _ = run_watch(monkeypatch, capsys, env, "--max", "10", "--max-failures", "3", on_sleep=outage)
    assert code == 0, out
    assert state["failed"] == 2
    assert "transient failure 1/3" in out and "transient failure 2/3" in out
    assert "BLIND" not in out and "\nREPORTED " in out


def test_consecutive_failures_exit_blind(env, monkeypatch, capsys):
    commit(env["work"], task_text("in_progress"), "taken", branch=BRANCH, push=True)
    state = {"fail": 0, "failed": 0}
    flaky_runner(monkeypatch, state)

    def outage(n):
        if n == 1:
            state["fail"] = 10 ** 6

    code, out, sleeps = run_watch(monkeypatch, capsys, env, "--max", "50", "--max-failures", "4", on_sleep=outage)
    assert code == 2
    assert state["failed"] == 4
    assert "BLIND: 4 consecutive failures" in out
    assert sleeps == 4  # 1 до збою + 3 повтори; вихід на 4-му збої поспіль
    assert no_watch_refs(env)


def test_success_resets_failure_counter(env, monkeypatch, capsys):
    commit(env["work"], task_text("in_progress"), "taken", branch=BRANCH, push=True)
    state = {"fail": 0, "failed": 0}
    flaky_runner(monkeypatch, state)

    def outage(n):
        if n in (1, 4):  # два збої, успіх, знову два збої — поспіль ніколи не 3
            state["fail"] = 2

    code, out, _ = run_watch(monkeypatch, capsys, env, "--max", "8", "--max-failures", "3", on_sleep=outage)
    assert code == 1 and out.rstrip().endswith("TIMEOUT")
    assert state["failed"] == 4


def test_start_self_check_retries_then_ok(env, monkeypatch, capsys):
    state = {"fail": 2, "failed": 0}
    flaky_runner(monkeypatch, state)
    code, out, _ = run_watch(monkeypatch, capsys, env, "--max", "1")
    assert code == 1 and "self-check ok" in out
    assert "self-check attempt 1/3 failed" in out and "self-check attempt 2/3 failed" in out


def test_start_self_check_fails_strictly(env, monkeypatch, capsys):
    state = {"fail": 10 ** 6, "failed": 0}
    flaky_runner(monkeypatch, state)
    code, out, _ = run_watch(monkeypatch, capsys, env, "--max", "5")
    assert code == 2
    assert "SELF-CHECK FAILED after 3 attempt(s)" in out
    assert state["failed"] == 3


def test_missing_probe_branch_fails_without_retry(env, monkeypatch, capsys):
    code, out, sleeps = run_watch(monkeypatch, capsys, env, "--self-check", "no-such-branch")
    assert code == 2 and "SELF-CHECK FAILED: origin has no 'no-such-branch'" in out
    assert sleeps == 0


def test_killed_watcher_leaves_no_ref(env, monkeypatch, capsys):
    """Сторож, убитий посеред очікування (TaskStop, перезапуск сесії), не лишає ref на стару історію."""
    commit(env["work"], task_text("in_progress"), "taken", branch=BRANCH, push=True)

    def kill(n):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_watch(monkeypatch, capsys, env, "--max", "5", on_sleep=kill)
    assert no_watch_refs(env)
