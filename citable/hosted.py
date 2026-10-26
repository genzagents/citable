#!/usr/bin/env python3
"""citable hosted: the paid tier, with no server to run.

A subscriber pays on Polar and types their site(s), prompts and brand into the checkout's custom fields.
This runner (a cron or launchd job on any always-on machine) polls Polar for active subscriptions, runs
the watch (and the prompt tracking) for each, keeps a small state file per subscriber, and emails:

  * a first report within one run of paying (so the product lands minutes after the card does),
  * an alert the moment something regresses (an answer bot blocked, llms.txt gone, site down, score drop,
    an engine that stopped citing them),
  * a weekly digest otherwise.

    python3 -m citable.hosted run                       # one pass against Polar + the email provider
    python3 -m citable.hosted run --subscribers=f.json  # one pass against a local list (no Polar)
    python3 -m citable.hosted run --dry-run             # never sends: prints every email it would send
    python3 -m citable.hosted launchd                   # prints a launchd plist that runs `run` hourly

Environment:
  POLAR_ACCESS_TOKEN   organisation access token (subscriptions:read). Required unless --subscribers.
  POLAR_PRODUCT_ID     optional: only subscriptions of this product.
  POLAR_SANDBOX=1      use sandbox-api.polar.sh.
  RESEND_API_KEY       email provider key. Required unless --dry-run.
  CITABLE_FROM         sender, e.g. "citable <watch@example.com>" (a verified Resend domain).
  CITABLE_STATE_DIR    where state lives (default ~/.citable-hosted).
  CITABLE_LIMITS       "sites,prompts" per subscriber (default "5,10", what the page promises).
  ANTHROPIC_API_KEY / OPENAI_API_KEY / PERPLEXITY_API_KEY: engines for prompt tracking (citable.track).

Polar custom field slugs read from each subscription's custom_field_data: `site` (or sites/domain/url;
one per line or comma), `prompts` (one per line), `brand` (optional). Zero dependencies.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from citable import __version__, check_site, format_report  # noqa: E402
from citable import track as T  # noqa: E402
from citable import watch as W  # noqa: E402

ACTIVE = {"active", "trialing", "past_due"}
WEEK = 7 * 24 * 3600
SITE_KEYS = ("site", "sites", "domain", "domains", "website", "url", "urls")
PROMPT_KEYS = ("prompts", "prompt", "queries", "questions")
BRAND_KEYS = ("brand", "brand_name", "company")


# ----------------------------------------------------------------------------- pure logic (tested)

def _split(text: Any) -> List[str]:
    if not isinstance(text, str):
        return []
    return [s.strip() for s in re.split(r"[\n,;]+", text) if s.strip()]


def _pick(data: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    low = {str(k).lower().strip(): v for k, v in (data or {}).items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def limits() -> Tuple[int, int]:
    raw = os.environ.get("CITABLE_LIMITS", "5,10")
    try:
        a, b = [int(x) for x in raw.split(",")]
        return max(1, a), max(0, b)
    except ValueError:
        return 5, 10


def normalise(sub: Dict[str, Any], max_sites: int = 5, max_prompts: int = 10) -> Optional[Dict[str, Any]]:
    """Polar subscription -> {id, email, name, sites, prompts, brand}. None when not active or no email/site."""
    if sub.get("status") not in ACTIVE:
        return None
    customer = sub.get("customer") or {}
    email = (customer.get("email") or sub.get("email") or "").strip()
    data = sub.get("custom_field_data") or {}
    sites = [W.host_of(s) for s in _split(_pick(data, SITE_KEYS))][:max_sites]
    sites = list(dict.fromkeys(s for s in sites if "." in s))
    if not email or not sites:
        return None
    prompts = _split(_pick(data, PROMPT_KEYS))[:max_prompts]
    brand = _pick(data, BRAND_KEYS)
    return {
        "id": str(sub.get("id") or email),
        "email": email,
        "name": (customer.get("name") or "").strip(),
        "sites": sites,
        "prompts": prompts,
        "brand": str(brand).strip() if isinstance(brand, str) else "",
        "period_end": sub.get("current_period_end") or "",
    }


def decide(state: Optional[Dict[str, Any]], worse: List[str], better: List[str], now: float, tracked: bool = False) -> str:
    """What to email this run: 'first', 'alert', 'digest' or ''. A fresh prompt-tracking run is always reported."""
    if not state or not state.get("last_report_at"):
        return "first"
    if worse or better:
        return "alert"
    if tracked or now - float(state.get("last_report_at", 0)) >= WEEK:
        return "digest"
    return ""


def subject(kind: str, sub: Dict[str, Any], results: List[Dict[str, Any]], worse: List[str]) -> str:
    hosts = ", ".join(sub["sites"])
    if kind == "first":
        return f"Your first citable report: {hosts}"
    if kind == "alert":
        return f"citable alert: {worse[0] if worse else 'something improved'}"
    scores = " / ".join(f"{r['score']}" if r.get("reachable") else "down" for r in results)
    return f"citable weekly: {hosts} ({scores})"


def render_email(kind: str, sub: Dict[str, Any], results: List[Dict[str, Any]], worse: List[str], better: List[str],
                 tracks: List[Dict[str, Any]]) -> Tuple[str, str]:
    """(text, html) bodies. Plain text first; HTML is the same text in a readable wrapper (no dependencies)."""
    name = sub.get("name") or "there"
    lines: List[str] = [f"Hi {name},", ""]
    if kind == "first":
        lines += ["This is your first citable report. From now on you get an email the moment something regresses",
                  "(an answer bot blocked, llms.txt gone, the site down, a score drop, an engine that stops citing you)",
                  "and a digest once a week otherwise.", ""]
    if worse:
        lines += ["WHAT GOT WORSE"] + [f"  - {w}" for w in worse] + [""]
    if better:
        lines += ["WHAT GOT BETTER"] + [f"  - {b}" for b in better] + [""]
    for r in results:
        lines += [format_report(r).rstrip(), ""]
    for t in tracks:
        lines += [T.render_markdown(t).rstrip(), ""]
    lines += ["--", f"citable {__version__} hosted watch. Reply to this email to change your sites or prompts;",
              "manage or cancel the subscription from the Polar receipt. https://genzagents.github.io/citable/"]
    text = "\n".join(lines)
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = ("<div style=\"font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.45;white-space:pre-wrap;"
            "max-width:760px\">" + esc + "</div>")
    return text, html


# ----------------------------------------------------------------------------- I/O

def _http(method: str, url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json", **headers})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    return json.loads(raw) if raw.strip() else {}


def polar_subscriptions() -> List[Dict[str, Any]]:
    token = os.environ.get("POLAR_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("POLAR_ACCESS_TOKEN is not set (or pass --subscribers=file.json)")
    base = "https://sandbox-api.polar.sh" if os.environ.get("POLAR_SANDBOX") else "https://api.polar.sh"
    product = os.environ.get("POLAR_PRODUCT_ID", "")
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        url = f"{base}/v1/subscriptions/?limit=100&page={page}" + (f"&product_id={product}" if product else "")
        res = _http("GET", url, {"Authorization": f"Bearer {token}"})
        items = res.get("items") or []
        out += items
        if page >= int((res.get("pagination") or {}).get("max_page") or 1) or not items:
            return out
        page += 1


def send_email(to: str, subj: str, text: str, html: str) -> str:
    key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("CITABLE_FROM", "citable <onboarding@resend.dev>")
    if not key:
        raise RuntimeError("RESEND_API_KEY is not set (use --dry-run to print emails instead)")
    res = _http("POST", "https://api.resend.com/emails", {"Authorization": f"Bearer {key}"},
                {"from": sender, "to": [to], "subject": subj, "text": text, "html": html})
    return str(res.get("id", ""))


def state_dir() -> str:
    d = os.environ.get("CITABLE_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".citable-hosted")
    os.makedirs(d, exist_ok=True)
    return d


def load_state(sub_id: str) -> Optional[Dict[str, Any]]:
    p = os.path.join(state_dir(), re.sub(r"[^A-Za-z0-9_.-]", "_", sub_id) + ".json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_state(sub_id: str, state: Dict[str, Any]) -> None:
    p = os.path.join(state_dir(), re.sub(r"[^A-Za-z0-9_.-]", "_", sub_id) + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def process(sub: Dict[str, Any], now: float, live: bool = True, do_track: bool = True,
            engines: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run one subscriber: check every site, track when due, diff against state, return what to send (not sent)."""
    state = load_state(sub["id"]) or {}
    results: List[Dict[str, Any]] = []
    worse: List[str] = []
    better: List[str] = []
    new_checks: Dict[str, Any] = {}
    for site in sub["sites"]:
        r = check_site(site, live=live)
        results.append(r)
        snap = W.snapshot(r)
        w, b = W.diff((state.get("checks") or {}).get(site), snap)
        worse += [f"{site}: {x}" for x in w]
        better += [f"{site}: {x}" for x in b]
        new_checks[site] = snap
    tracks: List[Dict[str, Any]] = []
    new_tracks: Dict[str, Any] = dict(state.get("tracks") or {})
    engines = engines if engines is not None else T.available_engines()
    due = not state.get("last_track_at") or now - float(state.get("last_track_at", 0)) >= WEEK
    if do_track and sub["prompts"] and engines and due:
        for site in sub["sites"]:
            t = T.track(site, sub["prompts"], engines, brand=sub.get("brand", ""))
            tracks.append(t)
            snap = T.snapshot(site, t["results"])
            w, b = T.diff(new_tracks.get(site), snap)
            worse += [f"{site}: {x}" for x in w]
            better += [f"{site}: {x}" for x in b]
            new_tracks[site] = snap
    kind = decide(state, worse, better, now, tracked=bool(tracks))
    new_state = {**state, "email": sub["email"], "sites": sub["sites"], "checks": new_checks, "tracks": new_tracks,
                 "last_run_at": now, "version": __version__}
    if tracks:
        new_state["last_track_at"] = now
    return {"kind": kind, "results": results, "tracks": tracks, "worse": worse, "better": better, "state": new_state}


def run(subscribers: List[Dict[str, Any]], dry_run: bool, live: bool = True, do_track: bool = True,
        now: Optional[float] = None, out=sys.stdout) -> int:
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    sent = 0
    for sub in subscribers:
        try:
            res = process(sub, now, live=live, do_track=do_track)
        except Exception as e:  # noqa: BLE001 - one subscriber's failure must not stop the others
            print(f"hosted: {sub['email']}: {e}", file=sys.stderr)
            continue
        kind = res["kind"]
        if kind:
            subj = subject(kind, sub, res["results"], res["worse"])
            text, html = render_email(kind, sub, res["results"], res["worse"], res["better"], res["tracks"])
            if dry_run:
                print(f"=== would send [{kind}] to {sub['email']}: {subj}\n{text}\n", file=out)
            else:
                msg_id = send_email(sub["email"], subj, text, html)
                print(f"hosted: sent [{kind}] to {sub['email']} ({msg_id}): {subj}", file=out)
            res["state"]["last_report_at"] = now
            res["state"]["last_kind"] = kind
            sent += 1
        else:
            print(f"hosted: {sub['email']}: no change, next digest in "
                  f"{max(0, int((WEEK - (now - float(res['state'].get('last_report_at', now)))) / 3600))}h", file=out)
        save_state(sub["id"], res["state"])
    print(f"hosted: {len(subscribers)} subscriber(s), {sent} email(s){' (dry run)' if dry_run else ''}", file=out)
    return 0


LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.genzagents.citable.hosted</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>-m</string><string>citable.hosted</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{home}/.citable-hosted/run.log</string>
  <key>StandardErrorPath</key><string>{home}/.citable-hosted/run.err</string>
</dict></plist>
<!-- Secrets: put POLAR_ACCESS_TOKEN, RESEND_API_KEY, CITABLE_FROM and engine keys in the EnvironmentVariables dict
     (or source them from the keychain in a wrapper script). Install:
       cp this ~/Library/LaunchAgents/io.genzagents.citable.hosted.plist && launchctl load -w ~/Library/LaunchAgents/io.genzagents.citable.hosted.plist -->
"""


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
    cmd, flags = argv[0], [a for a in argv[1:] if a.startswith("--")]
    if cmd == "launchd":
        print(LAUNCHD.format(python=sys.executable, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             home=os.path.expanduser("~")))
        return 0
    if cmd != "run":
        print(f"unknown command {cmd!r}; see --help", file=sys.stderr)
        return 2
    max_sites, max_prompts = limits()
    src = _flag(flags, "--subscribers")
    if src:
        with open(src, encoding="utf-8") as f:
            raw = json.load(f)
        raw = raw.get("items", raw) if isinstance(raw, dict) else raw
    else:
        raw = polar_subscriptions()
    subs = [s for s in (normalise(x, max_sites, max_prompts) for x in raw) if s]
    skipped = len(raw) - len(subs)
    if skipped:
        print(f"hosted: {skipped} subscription(s) skipped (not active, or no email/site in custom fields)", file=sys.stderr)
    return run(subs, dry_run="--dry-run" in flags, live="--no-live" not in flags, do_track="--no-track" not in flags)


if __name__ == "__main__":
    sys.exit(main())
