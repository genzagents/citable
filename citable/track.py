#!/usr/bin/env python3
"""citable track: does ChatGPT, Claude or Perplexity actually cite your site when people ask the
questions you want to be the answer to? Ask each engine your prompts, record which domains it cites,
and keep a GitHub issue up to date so you get an email the week a citation is lost.

    python3 -m citable.track DOMAIN "best X for Y" "how do I Z" [--engines=claude-code,anthropic,openai,perplexity]
                                   [--prompts-file=prompts.txt] [--brand="Name"] [--json] [--issue] [--repo=OWNER/NAME]

Engines and what they need:
    claude-code   the `claude` CLI on PATH (Claude Code, uses its WebSearch tool; no API key)
    anthropic     ANTHROPIC_API_KEY   (Messages API with the web_search server tool; --model, default claude-opus-5-5)
    openai        OPENAI_API_KEY      (Responses API with the web_search tool; --openai-model, default gpt-5)
    perplexity    PERPLEXITY_API_KEY  (chat completions, model sonar; --perplexity-model)
With no --engines, every engine whose key (or CLI) is present runs. Raw HTTP on purpose: zero dependencies.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from citable import __version__  # noqa: E402
from citable.watch import TITLE_PREFIX, find_issue, gh, parse_state  # noqa: E402

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
ENGINES = ("claude-code", "anthropic", "openai", "perplexity")


# ----------------------------------------------------------------------------- pure logic (tested)

def norm_host(url_or_host: str) -> str:
    h = url_or_host.strip().lower()
    if "://" in h:
        h = urllib.parse.urlsplit(h).netloc
    h = h.split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def same_site(url: str, domain: str) -> bool:
    h, d = norm_host(url), norm_host(domain)
    return h == d or h.endswith("." + d)


def urls_in(text: str) -> List[str]:
    return [u.rstrip(".,;:!?") for u in URL_RE.findall(text or "")]


def judge(domain: str, text: str, cited_urls: List[str], brand: str = "") -> Dict[str, Any]:
    """Was the site cited (a URL on the domain), or merely mentioned (domain or brand in the text)?"""
    all_urls = list(dict.fromkeys(cited_urls + urls_in(text)))
    ours = [u for u in all_urls if same_site(u, domain)]
    hosts = Counter(norm_host(u) for u in all_urls if not same_site(u, domain))
    low = (text or "").lower()
    mentioned = norm_host(domain) in low or (bool(brand) and brand.lower() in low)
    return {"cited": bool(ours), "mentioned": mentioned or bool(ours), "our_urls": ours[:5],
            "other_hosts": hosts.most_common(10), "excerpt": re.sub(r"\s+", " ", (text or "")[:280])}


def parse_anthropic(resp: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Text plus cited URLs from a Messages API response that used the web_search server tool."""
    text, cited = [], []
    for block in resp.get("content", []):
        if block.get("type") == "text":
            text.append(block.get("text", ""))
            for c in block.get("citations") or []:
                if c.get("url"):
                    cited.append(c["url"])
    return "\n".join(text), cited


def parse_openai(resp: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Text plus url_citation annotations from a Responses API result."""
    text, cited = [], []
    for item in resp.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                text.append(part.get("text", ""))
                for a in part.get("annotations") or []:
                    if a.get("type") == "url_citation" and a.get("url"):
                        cited.append(a["url"])
    return "\n".join(text), cited


def parse_perplexity(resp: Dict[str, Any]) -> Tuple[str, List[str]]:
    text = ""
    try:
        text = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        pass
    cited = [u for u in resp.get("citations") or [] if isinstance(u, str)]
    cited += [r.get("url") for r in resp.get("search_results") or [] if isinstance(r, dict) and r.get("url")]
    return text, list(dict.fromkeys(cited))


def visibility(results: List[Dict[str, Any]]) -> int:
    ok = [r for r in results if not r.get("error")]
    return round(100 * sum(1 for r in ok if r["cited"]) / len(ok)) if ok else 0


def snapshot(domain: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    pairs = {f"{r['engine']}|{r['prompt']}": ("error" if r.get("error") else r["cited"]) for r in results}
    return {"domain": norm_host(domain), "visibility": visibility(results), "pairs": pairs}


def diff(prev: Optional[Dict[str, Any]], cur: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    worse, better = [], []
    if not prev:
        return worse, better
    pp = prev.get("pairs", {})
    for key, now in cur["pairs"].items():
        before = pp.get(key)
        if before is None or now == "error" or before == "error":
            continue
        engine, prompt = key.split("|", 1)
        if before and not now:
            worse.append(f"{engine} no longer cites you for \"{prompt}\".")
        elif now and not before:
            better.append(f"{engine} now cites you for \"{prompt}\".")
    if cur["visibility"] < prev.get("visibility", 0):
        worse.append(f"Visibility fell from {prev['visibility']}% to {cur['visibility']}%.")
    elif cur["visibility"] > prev.get("visibility", 0):
        better.append(f"Visibility rose from {prev['visibility']}% to {cur['visibility']}%.")
    return worse, better


# ----------------------------------------------------------------------------- engines

def _post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int = 180) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json", "user-agent": f"citable/{__version__}", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def ask_anthropic(prompt: str, opts: Dict[str, str]) -> Tuple[str, List[str]]:
    resp = _post("https://api.anthropic.com/v1/messages",
                 {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                  "anthropic-beta": "server-side-fallback-2026-07-01"},
                 {"model": opts.get("model") or "claude-opus-5-5", "max_tokens": 4096,
                  "output_config": {"effort": "low"}, "fallbacks": "default",
                  "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
                  "messages": [{"role": "user", "content": prompt}]})
    if resp.get("stop_reason") == "refusal":
        raise RuntimeError("refused: " + str((resp.get("stop_details") or {}).get("category")))
    return parse_anthropic(resp)


def ask_openai(prompt: str, opts: Dict[str, str]) -> Tuple[str, List[str]]:
    resp = _post("https://api.openai.com/v1/responses",
                 {"authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                 {"model": opts.get("openai_model") or "gpt-5", "tools": [{"type": "web_search"}], "input": prompt})
    return parse_openai(resp)


def ask_perplexity(prompt: str, opts: Dict[str, str]) -> Tuple[str, List[str]]:
    resp = _post("https://api.perplexity.ai/chat/completions",
                 {"authorization": "Bearer " + os.environ["PERPLEXITY_API_KEY"]},
                 {"model": opts.get("perplexity_model") or "sonar", "messages": [{"role": "user", "content": prompt}]})
    return parse_perplexity(resp)


def ask_claude_code(prompt: str, opts: Dict[str, str]) -> Tuple[str, List[str]]:
    cmd = [shutil.which("claude") or "claude", "-p",
           prompt + "\n\nSearch the web before answering. Answer the way you would for any user, then end with a line "
                    "'Sources:' followed by the URLs you relied on, one per line.",
           "--allowedTools", "WebSearch", "--output-format", "json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       cwd=opts.get("cwd") or os.path.expanduser("~"))
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {p.stderr.strip()[:200]}")
    try:
        d = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError("claude returned no JSON: " + p.stdout[:200]) from None
    if d.get("is_error"):
        raise RuntimeError("claude error: " + str(d.get("result"))[:200])
    text = d.get("result") or ""
    return text, urls_in(text)


ASK = {"anthropic": ask_anthropic, "openai": ask_openai, "perplexity": ask_perplexity, "claude-code": ask_claude_code}


def available_engines() -> List[str]:
    out = []
    if shutil.which("claude"):
        out.append("claude-code")
    for name, var in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"), ("perplexity", "PERPLEXITY_API_KEY")):
        if os.environ.get(var):
            out.append(name)
    return out


def run_one(domain: str, prompt: str, engine: str, brand: str, opts: Dict[str, str]) -> Dict[str, Any]:
    base = {"engine": engine, "prompt": prompt}
    try:
        text, cited = ASK[engine](prompt, opts)
    except Exception as e:  # noqa: BLE001 - every failure becomes a row, the run goes on
        return {**base, "error": str(e)[:300], "cited": False, "mentioned": False, "our_urls": [], "other_hosts": [], "excerpt": ""}
    return {**base, **judge(domain, text, cited, brand)}


def track(domain: str, prompts: List[str], engines: List[str], brand: str = "", opts: Optional[Dict[str, str]] = None,
          workers: int = 4) -> Dict[str, Any]:
    opts = opts or {}
    jobs = [(p, e) for p in prompts for e in engines]
    results: List[Dict[str, Any]] = [{}] * len(jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(run_one, domain, p, e, brand, opts): i for i, (p, e) in enumerate(jobs)}
        for f in concurrent.futures.as_completed(futs):
            results[futs[f]] = f.result()
    competitors: Counter = Counter()
    for r in results:
        for host, n in r.get("other_hosts", []):
            competitors[host] += 1  # count answers, not links
    return {"domain": norm_host(domain), "brand": brand, "prompts": prompts, "engines": engines, "results": results,
            "visibility": visibility(results), "competitors": competitors.most_common(10), "version": __version__}


# ----------------------------------------------------------------------------- output

def render_markdown(t: Dict[str, Any], run_url: str = "") -> str:
    out = [f"## citable track: {t['domain']}", "",
           f"**Visibility {t['visibility']}%**: share of prompt-and-engine pairs where an answer linked to your site."
           + (f" [run]({run_url})" if run_url else ""), "",
           "| Prompt | " + " | ".join(t["engines"]) + " |", "|---|" + "---|" * len(t["engines"])]
    by = {(r["engine"], r["prompt"]): r for r in t["results"]}
    for p in t["prompts"]:
        cells = []
        for e in t["engines"]:
            r = by.get((e, p), {})
            cells.append("error" if r.get("error") else "**cited**" if r.get("cited") else "mentioned" if r.get("mentioned") else "no")
        out.append(f"| {p} | " + " | ".join(cells) + " |")
    if t["competitors"]:
        out += ["", "**Who gets cited instead** (answers citing them): " + ", ".join(f"{h} ({n})" for h, n in t["competitors"][:8])]
    errs = [r for r in t["results"] if r.get("error")]
    if errs:
        out += ["", "**Errors**", ""] + [f"- {r['engine']}, \"{r['prompt']}\": {r['error']}" for r in errs[:10]]
    return "\n".join(out)


def render_body(t: Dict[str, Any], snap: Dict[str, Any], run_url: str) -> str:
    return (render_markdown(t, run_url) + "\n\n<sub>Kept up to date by [citable track](https://github.com/genzagents/citable#track) "
            f"{__version__}. It comments here when a citation is lost or gained, so subscribe to this issue to get the email.</sub>\n\n"
            f"<!-- citable-state {json.dumps(snap, separators=(',', ':'))} -->")


def upsert_issue(repo: str, t: Dict[str, Any], snap: Dict[str, Any], notify: str, run_url: str, label: str) -> Dict[str, Any]:
    title = TITLE_PREFIX.replace("watch", "track") + t["domain"]
    existing = find_issue(repo, title)
    prev = parse_state(existing["body"]) if existing else None
    worse, better = diff(prev, snap)
    body = render_body(t, snap, run_url)
    if existing is None:
        try:
            gh("label", "create", label, "--repo", repo, "--color", "5319e7", "--description", "citable results", "--force")
        except RuntimeError:
            pass
        out = gh("issue", "create", "--repo", repo, "--title", title, "--body", body, "--label", label)
        return {"number": int(out.strip().rsplit("/", 1)[-1]), "created": True, "worse": worse, "better": better}
    number = int(existing["number"])
    gh("issue", "edit", str(number), "--repo", repo, "--body", body)
    if worse or better:
        if worse and existing.get("state", "").upper() == "CLOSED":
            gh("issue", "reopen", str(number), "--repo", repo)
        lines = []
        if worse:
            lines.append((f"{notify} " if notify else "") + f"**Lost citations on {t['domain']}**")
            lines += [f"- {w}" for w in worse]
        if better:
            lines.append("**Gained**")
            lines += [f"- {b}" for b in better]
        lines.append(f"\nVisibility now {t['visibility']}%." + (f" Full report: {run_url}" if run_url else ""))
        gh("issue", "comment", str(number), "--repo", repo, "--body", "\n".join(lines))
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
    args = [a for a in argv if not a.startswith("--")]
    domain, prompts = args[0], [p.strip() for p in args[1:] if p.strip()]
    pf = _flag(flags, "--prompts-file")
    if pf:
        with open(pf, encoding="utf-8") as f:
            prompts += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if not prompts:
        print("track: give at least one prompt (positional or --prompts-file)", file=sys.stderr)
        return 2
    engines = [e for e in _flag(flags, "--engines").split(",") if e] or available_engines()
    bad = [e for e in engines if e not in ENGINES]
    if bad:
        print(f"track: unknown engine(s) {bad}; choose from {ENGINES}", file=sys.stderr)
        return 2
    if not engines:
        print("track: no engine available: install Claude Code (claude on PATH) or set ANTHROPIC_API_KEY, OPENAI_API_KEY or PERPLEXITY_API_KEY", file=sys.stderr)
        return 2
    opts = {"model": _flag(flags, "--model"), "openai_model": _flag(flags, "--openai-model"),
            "perplexity_model": _flag(flags, "--perplexity-model")}
    t = track(domain, prompts, engines, _flag(flags, "--brand"), opts, workers=int(_flag(flags, "--workers", "4")))
    repo = _flag(flags, "--repo", os.environ.get("GITHUB_REPOSITORY", ""))
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID") and repo:
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    md = render_markdown(t, run_url)
    snap = snapshot(domain, t["results"])
    regressed = False
    if "--issue" in flags:
        if not repo:
            print("track: --issue needs --repo or GITHUB_REPOSITORY", file=sys.stderr)
        else:
            notify = _flag(flags, "--notify")
            notify = ("@" + notify) if notify and not notify.startswith("@") else notify
            try:
                res = upsert_issue(repo, t, snap, notify, run_url, _flag(flags, "--label", "citable"))
                t["issue"] = f"https://github.com/{repo}/issues/{res['number']}"
                regressed = bool(res["worse"])
                for w in res["worse"]:
                    print(f"  LOST: {w}", file=sys.stderr)
                for b in res["better"]:
                    print(f"  gained: {b}", file=sys.stderr)
            except RuntimeError as e:
                print(f"track: {e}", file=sys.stderr)
    if "--json" in flags:
        print(json.dumps(t, indent=2))
    else:
        print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"visibility={t['visibility']}\ncitations_lost={'true' if regressed else 'false'}\n")
    min_vis = int(_flag(flags, "--min-visibility", "0"))
    if t["visibility"] < min_vis:
        print(f"visibility {t['visibility']}% is below --min-visibility={min_vis}", file=sys.stderr)
        return 1
    return 1 if (regressed and "--no-fail-on-regression" not in flags) else 0


if __name__ == "__main__":
    sys.exit(main())
