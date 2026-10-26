#!/usr/bin/env python3
"""citable watch: check one or more sites, remember the result in a GitHub issue, and raise the alarm
(an issue comment, which GitHub emails to everyone subscribed) the moment something regresses:
an answer bot gets blocked, /llms.txt disappears, the site goes down, or the score drops.

    python3 -m citable.watch example.com other.com [--min-score=N] [--no-live] [--no-issue]
                                                   [--repo=OWNER/NAME] [--notify=@login]

Designed for GitHub Actions (see action.yml and docs/watch.yml) but runs anywhere `gh` is logged in.
Environment used when present: GH_TOKEN, GITHUB_REPOSITORY, GITHUB_STEP_SUMMARY, GITHUB_OUTPUT,
GITHUB_SERVER_URL, GITHUB_RUN_ID. Zero dependencies beyond the `gh` CLI for the issue part.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from citable import __version__, check_site, format_report  # noqa: E402

STATE_RE = re.compile(r"<!-- citable-state (\{.*?\}) -->", re.S)
TITLE_PREFIX = "citable watch: "


# ----------------------------------------------------------------------------- pure logic (tested)

def snapshot(r: Dict[str, Any]) -> Dict[str, Any]:
    """The few facts worth comparing between runs."""
    bots: Dict[str, str] = {}
    for name, e in r.get("bots", {}).items():
        if e.get("role") != "answer":
            continue
        bots[name] = "edge" if e.get("live_blocked") else ("robots" if e.get("robots") == "block" else "ok")
    return {
        "score": int(r.get("score", 0)),
        "grade": r.get("grade", "F"),
        "reachable": bool(r.get("reachable")),
        "bots": bots,
        "llms_txt": bool(r.get("llms_txt", {}).get("present")),
        "failing": sorted(c["id"] for c in r.get("checks", []) if c.get("status") == "fail"),
        "titles": {c["id"]: c["title"] for c in r.get("checks", [])},
        "checked_at": r.get("checked_at"),
    }


def diff(prev: Optional[Dict[str, Any]], cur: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Returns (worse, better): human sentences describing what changed since the previous snapshot."""
    worse: List[str] = []
    better: List[str] = []
    if not prev:
        return worse, better
    if prev.get("reachable") and not cur["reachable"]:
        worse.append("The site is unreachable (it was reachable last time).")
        return worse, better
    if not prev.get("reachable") and cur["reachable"]:
        better.append("The site is reachable again.")
    pbots = prev.get("bots", {})
    for bot, state in cur["bots"].items():
        before = pbots.get(bot, "ok")
        if state != "ok" and before == "ok":
            how = "at the edge (CDN/WAF returns an error to it)" if state == "edge" else "by robots.txt"
            worse.append(f"{bot} is now blocked {how}.")
        elif state == "ok" and before != "ok":
            better.append(f"{bot} is no longer blocked.")
    if prev.get("llms_txt") and not cur["llms_txt"]:
        worse.append("/llms.txt is gone (it was present last time).")
    elif not prev.get("llms_txt") and cur["llms_txt"]:
        better.append("/llms.txt is now present.")
    titles = {**prev.get("titles", {}), **cur.get("titles", {})}
    for cid in sorted(set(cur["failing"]) - set(prev.get("failing", []))):
        if cid in ("edge", "robots", "llms_txt"):
            continue  # already reported above in plainer words
        worse.append(f"Now failing: {titles.get(cid, cid)}.")
    for cid in sorted(set(prev.get("failing", [])) - set(cur["failing"])):
        if cid in ("edge", "robots", "llms_txt"):
            continue
        better.append(f"Fixed: {titles.get(cid, cid)}.")
    if cur["score"] < prev.get("score", 0):
        worse.append(f"Score fell from {prev['score']} to {cur['score']}.")
    elif cur["score"] > prev.get("score", 0):
        better.append(f"Score rose from {prev['score']} to {cur['score']}.")
    return worse, better


def parse_state(body: str) -> Optional[Dict[str, Any]]:
    m = STATE_RE.search(body or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def host_of(url: str) -> str:
    u = url if "://" in url else "https://" + url
    p = urllib.parse.urlsplit(u)
    return (p.netloc + (p.path.rstrip("/") if p.path not in ("", "/") else "")).lower()


def issue_title(url: str) -> str:
    return TITLE_PREFIX + host_of(url)


def render_body(r: Dict[str, Any], snap: Dict[str, Any], run_url: str = "") -> str:
    out = [f"## {issue_title(r['url'])}", ""]
    if not r.get("reachable"):
        out.append(f"**Unreachable**: {r.get('error')}")
    else:
        line = f"**Score {r['score']}/100, grade {r['grade']}** · checked {r.get('checked_at', '')}"
        if run_url:
            line += f" · [run]({run_url})"
        out += [line, "", "| Answer bot | robots.txt | live fetch |", "|---|---|---|"]
        for name, e in r["bots"].items():
            if e.get("role") != "answer":
                continue
            live = ""
            if "live_status" in e:
                live = f"HTTP {e['live_status']}" if e["live_status"] else "error"
                if e.get("live_blocked"):
                    live += " **BLOCKED**"
            out.append(f"| {name} | {e['robots']} | {live} |")
        bad = [c for c in r["checks"] if c["status"] in ("fail", "warn")]
        if bad:
            out += ["", "**Needs fixing**", ""]
            for c in bad:
                fix = f" Fix: {c['fix']}" if c.get("fix") else ""
                out.append(f"- [{c['status'].upper()}] {c['title']}: {c['detail']}{fix}")
    out += ["", f"<sub>Kept up to date by [citable watch](https://github.com/genzagents/citable#watch) "
                f"{__version__}. It comments here only when something changes, so subscribe to this issue "
                f"to get an email the moment an AI bot is blocked or your llms.txt breaks.</sub>",
            "", f"<!-- citable-state {json.dumps(snap, separators=(',', ':'))} -->"]
    return "\n".join(out)


def render_comment(worse: List[str], better: List[str], r: Dict[str, Any], notify: str, run_url: str) -> str:
    out = []
    if worse:
        mention = f"{notify} " if notify else ""
        out.append(f"{mention}**Something regressed on {host_of(r['url'])}**")
        out += [f"- {w}" for w in worse]
    if better:
        out.append("**Improved**" if not worse else "")
        out += [f"- {b}" for b in better]
    if r.get("reachable"):
        out.append(f"\nScore now {r['score']}/100 ({r['grade']}).")
    if run_url:
        out.append(f"Full report: {run_url}")
    return "\n".join(x for x in out if x is not None)


# ----------------------------------------------------------------------------- GitHub glue (gh CLI)

def gh(*args: str, input_: Optional[str] = None) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, input=input_)
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()[:400]}")
    return p.stdout


def find_issue(repo: str, title: str) -> Optional[Dict[str, Any]]:
    raw = gh("issue", "list", "--repo", repo, "--state", "all", "--limit", "100",
             "--search", f'"{title}" in:title', "--json", "number,title,body,state")
    for it in json.loads(raw or "[]"):
        if it["title"].strip().lower() == title.lower():
            return it
    return None


def upsert_issue(repo: str, r: Dict[str, Any], snap: Dict[str, Any], notify: str, run_url: str,
                 label: str = "citable") -> Dict[str, Any]:
    title = issue_title(r["url"])
    body = render_body(r, snap, run_url)
    existing = find_issue(repo, title)
    prev = parse_state(existing["body"]) if existing else None
    worse, better = diff(prev, snap)
    if existing is None:
        try:
            gh("label", "create", label, "--repo", repo, "--color", "5319e7",
               "--description", "citable watch results", "--force")
        except RuntimeError:
            pass
        out = gh("issue", "create", "--repo", repo, "--title", title, "--body", body, "--label", label)
        number = int(out.strip().rsplit("/", 1)[-1])
        return {"number": number, "created": True, "worse": worse, "better": better}
    number = int(existing["number"])
    gh("issue", "edit", str(number), "--repo", repo, "--body", body)
    if worse or better:
        if worse and existing.get("state", "").upper() == "CLOSED":
            gh("issue", "reopen", str(number), "--repo", repo)
        gh("issue", "comment", str(number), "--repo", repo, "--body", render_comment(worse, better, r, notify, run_url))
    return {"number": number, "created": False, "worse": worse, "better": better}


# ----------------------------------------------------------------------------- entry point

def _flag(flags: List[str], name: str, default: str = "") -> str:
    for f in flags:
        if f.startswith(name + "="):
            return f.split("=", 1)[1]
    return default


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    flags = [a for a in argv if a.startswith("--")]
    sites: List[str] = []
    for a in argv:
        if not a.startswith("--"):
            sites += [s for s in re.split(r"[\s,]+", a) if s]
    min_score = int(_flag(flags, "--min-score", "0"))
    live = "--no-live" not in flags
    use_issue = "--no-issue" not in flags
    repo = _flag(flags, "--repo", os.environ.get("GITHUB_REPOSITORY", ""))
    notify = _flag(flags, "--notify", "")
    if notify and not notify.startswith("@"):
        notify = "@" + notify
    label = _flag(flags, "--label", "citable")
    fail_on_regression = "--no-fail-on-regression" not in flags
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID") and repo:
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    summary_lines = [f"## citable watch {__version__}", "", "| Site | Score | Changed | Issue |", "|---|---|---|---|"]
    regressed = changed = False
    min_seen = 100
    rc = 0
    for site in sites:
        r = check_site(site, live=live)
        snap = snapshot(r)
        print(format_report(r))
        print()
        issue_cell = "off"
        worse: List[str] = []
        better: List[str] = []
        if use_issue:
            if not repo:
                print("watch: no --repo and no GITHUB_REPOSITORY; skipping the issue (use --no-issue to silence)", file=sys.stderr)
            else:
                try:
                    res = upsert_issue(repo, r, snap, notify, run_url, label)
                    worse, better = res["worse"], res["better"]
                    issue_cell = f"[#{res['number']}](https://github.com/{repo}/issues/{res['number']})" + (" (new)" if res["created"] else "")
                except RuntimeError as e:
                    issue_cell = f"error: {e}"
                    print(f"watch: {e}", file=sys.stderr)
        if worse:
            regressed = True
            for w in worse:
                print(f"  REGRESSION: {w}")
        if better:
            for b in better:
                print(f"  improved: {b}")
        changed = changed or bool(worse or better)
        if r.get("reachable"):
            min_seen = min(min_seen, int(r["score"]))
        else:
            min_seen = 0
            if not worse:
                rc = max(rc, 2)
        score_cell = f"{r['score']}/100 ({r['grade']})" if r.get("reachable") else "unreachable"
        change_cell = "; ".join(worse) if worse else ("; ".join(better) if better else "no")
        summary_lines.append(f"| {host_of(site)} | {score_cell} | {change_cell} | {issue_cell} |")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(summary_lines) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"score={min_seen}\nregressed={'true' if regressed else 'false'}\nchanged={'true' if changed else 'false'}\n")
    if regressed and fail_on_regression:
        rc = max(rc, 1)
    if min_seen < min_score:
        print(f"score {min_seen} is below --min-score={min_score}")
        rc = max(rc, 1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
