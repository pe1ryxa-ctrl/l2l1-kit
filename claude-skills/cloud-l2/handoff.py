#!/usr/bin/env python3
"""Channel between two L2 (Architect) sessions: main (on the owner's machine) and cloud.

The channel is docs/HANDOFF-cloud.md in the ecosystem meta repository:
a table "Хто що тримає" at the top, then entries, newest first.

Commands (run anywhere inside the meta repo; stdlib only):
  handoff.py add --side main|cloud --title TEXT (--body TEXT | --body-file PATH)
  handoff.py owner WORK OWNER            # set owner of the row whose Робота contains WORK (unique)
  handoff.py owner --add WORK OWNER      # add a new row
  handoff.py latest [--side main|cloud] [-n N]
  handoff.py watch --side main|cloud [--interval 180] [--max 60]
      prints and exits 0 when a NEW entry written by --side appears on origin
      (self-check first: fails with exit 2 if origin cannot be read)

add/owner: pull --ff-only, edit, commit only this file, push; if the push is rejected
(the other side pushed meanwhile) the edit is re-applied on the fresh file (up to 5 tries).
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import time

REL = os.path.join("docs", "HANDOFF-cloud.md")
ARROW = {"main": "основний → хмара", "cloud": "хмара → основний"}
HEADER_RE = re.compile(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}.*$", re.M)


def git(root, *args, check=True):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def find_root():
    d = os.getcwd()
    while True:
        if os.path.exists(os.path.join(d, REL)):
            return d
        up = os.path.dirname(d)
        if up == d:
            sys.exit(f"{REL} not found above {os.getcwd()}")
        d = up


def branch(root):
    return git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def now_label():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M")
    except Exception:  # no tzdata (e.g. bare Windows Python) -> UTC, labelled
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def split_top(text):
    """Return (head, rest): head = title + table block up to and including the first '---' after it."""
    m = re.search(r"^---[ \t]*$", text, re.M)
    if not m:
        raise ValueError("no '---' separator after the table")
    cut = m.end()
    return text[:cut], text[cut:]


def op_add(side, title, body):
    header = f"## {now_label()} — {ARROW[side]}"
    entry = f"{header}\n\n**{title}**\n\n{body.strip()}\n\n---"

    def apply(text):
        head, rest = split_top(text)
        return head.rstrip() + "\n\n" + entry + "\n\n" + rest.lstrip()

    return apply, f"docs(handoff): {ARROW[side]} — {title}"


def op_owner(work, owner, add):
    def apply(text):
        lines = text.split("\n")
        if add:
            idx = max(i for i, l in enumerate(lines) if l.startswith("|"))
            lines.insert(idx + 1, f"| {work} | {owner} |")
            return "\n".join(lines)
        hits = [i for i, l in enumerate(lines) if l.startswith("|") and work in l.split("|")[1]]
        if len(hits) != 1:
            raise ValueError(f"'{work}' matches {len(hits)} table rows, need exactly 1")
        cells = lines[hits[0]].split("|")
        lines[hits[0]] = f"|{cells[1]}| {owner} |"
        return "\n".join(lines)

    return apply, f"docs(handoff): власник — {work} → {owner}"


def commit_push(root, apply, message):
    path = os.path.join(root, REL)
    br = branch(root)
    if git(root, "status", "--porcelain", "--", REL).stdout.strip():
        sys.exit(f"{REL} has uncommitted changes — commit or discard them first")
    for attempt in range(1, 6):
        git(root, "pull", "-q", "--ff-only", "origin", br)
        write(path, apply(read(path)))
        git(root, "add", "--", REL)
        git(root, "commit", "-q", "-m", message + "\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        r = git(root, "push", "-q", "origin", f"HEAD:{br}", check=False)
        if r.returncode == 0:
            print(f"pushed ({attempt}): {git(root, 'log', '--oneline', '-1').stdout.strip()}")
            return
        # the other side pushed meanwhile: drop our commit, restore the file, retry on fresh origin
        git(root, "reset", "-q", "--soft", "HEAD~1")
        git(root, "reset", "-q", "--", REL)
        git(root, "checkout", "-q", "--", REL)
        time.sleep(2 * attempt)
    sys.exit("push failed 5 times — resolve by hand")


def entries(text, side=None):
    heads = list(HEADER_RE.finditer(text))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        chunk = text[h.start():end].strip().rstrip("-").strip()
        if side is None or ARROW[side] in h.group(0):
            out.append(chunk)
    return out


def origin_text(root):
    br = branch(root)
    git(root, "fetch", "-q", "origin", br)
    return git(root, "show", f"origin/{br}:{REL.replace(os.sep, '/')}").stdout


def cmd_watch(root, side, interval, max_iter):
    try:
        base = len(entries(origin_text(root), side))
    except Exception as e:  # self-check: the watcher must be able to see origin before it starts
        print(f"SELF-CHECK FAILED: {e}")
        sys.exit(2)
    print(f"watching {ARROW[side]}: {base} entries now", flush=True)
    for _ in range(max_iter):
        time.sleep(interval)
        try:
            cur = entries(origin_text(root), side)
        except Exception as e:
            print(f"fetch failed, retrying: {e}", flush=True)
            continue
        if len(cur) > base:
            print(f"NEW ENTRY ({base} -> {len(cur)}):\n")
            print(cur[0])
            return
    print("no new entries")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--side", choices=ARROW, required=True)
    a.add_argument("--title", required=True)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file")
    o = sub.add_parser("owner")
    o.add_argument("--add", action="store_true")
    o.add_argument("work")
    o.add_argument("owner")
    lt = sub.add_parser("latest")
    lt.add_argument("--side", choices=ARROW)
    lt.add_argument("-n", type=int, default=2)
    w = sub.add_parser("watch")
    w.add_argument("--side", choices=ARROW, required=True)
    w.add_argument("--interval", type=int, default=180)
    w.add_argument("--max", type=int, default=60)
    args = ap.parse_args()
    root = find_root()
    if args.cmd == "add":
        body = args.body if args.body is not None else read(args.body_file)
        commit_push(root, *op_add(args.side, args.title, body))
    elif args.cmd == "owner":
        commit_push(root, *op_owner(args.work, args.owner, args.add))
    elif args.cmd == "latest":
        git(root, "pull", "-q", "--ff-only", "origin", branch(root), check=False)
        for e in entries(read(os.path.join(root, REL)), args.side)[: args.n]:
            print(e + "\n")
    else:
        cmd_watch(root, args.side, args.interval, args.max)


if __name__ == "__main__":
    main()
