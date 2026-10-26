#!/usr/bin/env python3
"""citable: can ChatGPT, Claude and Perplexity actually read and cite your site?

One file, zero dependencies, Python 3.9+.

    citable example.com               human-readable report, score out of 100
    citable example.com --json        machine-readable
    citable example.com --llms-txt    print a starter llms.txt built from the homepage
    citable mcp                       run as an MCP server over stdio (tools: check_site, draft_llms_txt)
    citable watch a.com b.com         weekly watch: remembers results in a GitHub issue, comments on regressions
    citable track a.com "best X"      prompt tracking: does each AI engine cite you for the prompts that matter
    citable hosted run                the paid tier: Polar subscribers in, first report / alert / weekly digest emails out

It does not guess from robots.txt alone: it fetches the homepage *as* each AI bot and reports
edge blocks (Cloudflare, WAFs) that robots.txt never mentions.
"""
from __future__ import annotations

import concurrent.futures
import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.4.0"
__all__ = ["check_site", "draft_llms_txt", "parse_robots", "robots_verdict", "main"]

TIMEOUT = 12
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# name, vendor, role, live user-agent string (None = robots.txt only)
# role "answer": fetches pages to answer or cite in a live conversation. Blocking these means no citations.
# role "train": builds training corpora. Blocking these is a legitimate choice; reported, not penalised.
BOTS: List[Tuple[str, str, str, Optional[str]]] = [
    ("OAI-SearchBot", "OpenAI", "answer",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot"),
    ("ChatGPT-User", "OpenAI", "answer",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
    ("Claude-SearchBot", "Anthropic", "answer",
     "Mozilla/5.0 (compatible; Claude-SearchBot/1.0; +https://www.anthropic.com/claude-searchbot)"),
    ("Claude-User", "Anthropic", "answer",
     "Mozilla/5.0 (compatible; Claude-User/1.0; +https://www.anthropic.com/claude-user)"),
    ("PerplexityBot", "Perplexity", "answer",
     "Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"),
    ("Perplexity-User", "Perplexity", "answer",
     "Mozilla/5.0 (compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"),
    ("DuckAssistBot", "DuckDuckGo", "answer", None),
    ("Bingbot", "Microsoft (Copilot)", "answer", None),
    ("Googlebot", "Google (AI Overviews)", "answer", None),
    ("GPTBot", "OpenAI", "train",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"),
    ("ClaudeBot", "Anthropic", "train",
     "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    ("Google-Extended", "Google (Gemini)", "train", None),
    ("Applebot-Extended", "Apple", "train", None),
    ("CCBot", "Common Crawl", "train", None),
    ("meta-externalagent", "Meta", "train", None),
    ("Amazonbot", "Amazon", "train", None),
    ("Bytespider", "ByteDance", "train", None),
]

SSL_CTX = ssl.create_default_context()


# ----------------------------------------------------------------------------- fetching

def fetch(url: str, ua: str = BROWSER_UA, accept: str = "*/*", max_bytes: int = 2_000_000) -> Dict[str, Any]:
    """GET a URL. Never raises; returns a dict with status (0 on network error), final_url, headers, body, ms."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept, "Accept-Language": "en"})
    t0 = time.time()
    out: Dict[str, Any] = {"url": url, "status": 0, "final_url": url, "headers": {}, "body": "", "error": None, "ms": 0}
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            raw = r.read(max_bytes)
            out.update(status=r.status, final_url=r.geturl(), headers={k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read(max_bytes)
        except Exception:
            pass
        out.update(status=e.code, final_url=e.geturl() or url, headers={k.lower(): v for k, v in e.headers.items()})
    except Exception as e:  # DNS, TLS, timeout, refused
        raw = b""
        out["error"] = f"{type(e).__name__}: {e}"
    out["ms"] = int((time.time() - t0) * 1000)
    ctype = out["headers"].get("content-type", "")
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else "utf-8"
    try:
        out["body"] = raw.decode(enc, errors="replace")
    except LookupError:
        out["body"] = raw.decode("utf-8", errors="replace")
    return out


def normalise_url(target: str) -> str:
    target = target.strip()
    if not re.match(r"^https?://", target, re.I):
        target = "https://" + target
    p = urllib.parse.urlsplit(target)
    path = p.path or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def origin(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


# ----------------------------------------------------------------------------- robots.txt

def parse_robots(text: str) -> Dict[str, Any]:
    """Parse robots.txt into groups [{agents:[...], rules:[(allow:bool, pattern)]}] and sitemaps."""
    groups: List[Dict[str, Any]] = []
    sitemaps: List[str] = []
    cur: Optional[Dict[str, Any]] = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if cur is None or not last_was_agent:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(value.lower())
            last_was_agent = True
        elif field in ("allow", "disallow"):
            last_was_agent = False
            if cur is None:
                continue
            cur["rules"].append((field == "allow", value))
        elif field == "sitemap":
            last_was_agent = False
            if value:
                sitemaps.append(value)
        else:
            last_was_agent = False
    return {"groups": groups, "sitemaps": sitemaps}


def _pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "$":
            parts.append("$")
        else:
            parts.append(re.escape(ch))
    return re.compile("^" + "".join(parts))


def robots_verdict(robots: Dict[str, Any], bot: str, path: str = "/") -> str:
    """'allow', 'block' or 'unspecified' for a bot and path, RFC 9309 style (longest match, allow wins ties)."""
    bot_l = bot.lower()
    specific = [g for g in robots["groups"] if any(a != "*" and (a == bot_l or a in bot_l or bot_l in a) for a in g["agents"])]
    chosen = specific or [g for g in robots["groups"] if "*" in g["agents"]]
    if not chosen:
        return "unspecified"
    best: Optional[Tuple[int, bool]] = None
    for g in chosen:
        for allow, pattern in g["rules"]:
            if pattern == "":
                if not allow:  # "Disallow:" (empty) means allow everything
                    cand = (0, True)
                else:
                    continue
            elif _pattern_to_regex(pattern).match(path):
                cand = (len(pattern), allow)
            else:
                continue
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] and not best[1]):
                best = cand
    if best is None:
        return "allow"
    return "allow" if best[1] else "block"


# ----------------------------------------------------------------------------- HTML helpers

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", body)
    body = _TAG_RE.sub(" ", body)
    return html.unescape(re.sub(r"\s+", " ", body)).strip()


def _meta(body: str, name: str) -> Optional[str]:
    for attr in ("name", "property"):
        m = re.search(
            rf'(?is)<meta\b[^>]*\b{attr}\s*=\s*["\']?{re.escape(name)}["\']?[^>]*\bcontent\s*=\s*["\']([^"\']*)["\']', body
        ) or re.search(
            rf'(?is)<meta\b[^>]*\bcontent\s*=\s*["\']([^"\']*)["\'][^>]*\b{attr}\s*=\s*["\']?{re.escape(name)}["\']?', body
        )
        if m:
            return html.unescape(m.group(1)).strip()
    return None


def _title(body: str) -> Optional[str]:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else None


def _canonical(body: str) -> Optional[str]:
    m = re.search(r'(?is)<link\b[^>]*\brel\s*=\s*["\']?canonical["\']?[^>]*\bhref\s*=\s*["\']([^"\']+)["\']', body) or re.search(
        r'(?is)<link\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*\brel\s*=\s*["\']?canonical["\']?', body
    )
    return m.group(1).strip() if m else None


def _lang(body: str) -> Optional[str]:
    m = re.search(r'(?is)<html\b[^>]*\blang\s*=\s*["\']?([a-zA-Z-]+)', body)
    return m.group(1) if m else None


def _jsonld(body: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in re.finditer(r'(?is)<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', body):
        txt = m.group(1).strip()
        try:
            data = json.loads(txt)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict):
                if "@graph" in it and isinstance(it["@graph"], list):
                    out.extend(x for x in it["@graph"] if isinstance(x, dict))
                else:
                    out.append(it)
    return out


def _jsonld_types(items: List[Dict[str, Any]]) -> List[str]:
    types: List[str] = []
    for it in items:
        t = it.get("@type")
        if isinstance(t, list):
            types.extend(str(x) for x in t)
        elif t:
            types.append(str(t))
    return types


def _links(body: str, base: str) -> List[Tuple[str, str]]:
    """Same-origin links from the page: [(absolute_url, anchor_text)] deduped, in order."""
    seen = set()
    out: List[Tuple[str, str]] = []
    host = urllib.parse.urlsplit(base).netloc
    for m in re.finditer(r'(?is)<a\b[^>]*\bhref\s*=\s*["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', body):
        href, text = m.group(1).strip(), _strip_html(m.group(2))
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absu = urllib.parse.urljoin(base, href)
        p = urllib.parse.urlsplit(absu)
        if p.netloc != host or p.scheme not in ("http", "https"):
            continue
        clean = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") or "/", "", ""))
        if clean in seen or clean.rstrip("/") == base.rstrip("/"):
            continue
        if re.search(r"\.(png|jpe?g|gif|svg|pdf|zip|css|js)$", p.path, re.I):
            continue
        seen.add(clean)
        out.append((clean, text[:80]))
    return out


# ----------------------------------------------------------------------------- llms.txt

def validate_llms_txt(resp: Dict[str, Any]) -> Dict[str, Any]:
    body = resp["body"]
    ctype = resp["headers"].get("content-type", "")
    looks_html = body.lstrip()[:1] == "<" or "text/html" in ctype
    present = resp["status"] == 200 and body.strip() != "" and not looks_html
    lines = body.splitlines()
    return {
        "present": present,
        "status": resp["status"],
        "soft_404": resp["status"] == 200 and looks_html,
        "bytes": len(body.encode("utf-8", errors="replace")),
        "has_h1": present and any(l.startswith("# ") for l in lines[:5]),
        "has_summary": present and any(l.startswith("> ") for l in lines),
        "link_count": (len(re.findall(r"\[[^\]]+\]\((https?://[^)]+|/[^)]*)\)", body))
                       or len(re.findall(r"(?m)^\s*(?:[-*]\s*)?https?://\S+", body))) if present else 0,
    }


# ----------------------------------------------------------------------------- the check

def _check(id_: str, title: str, status: str, points: int, max_points: int, detail: str, fix: str = "") -> Dict[str, Any]:
    return {"id": id_, "title": title, "status": status, "points": points, "max": max_points, "detail": detail, "fix": fix}


def check_site(target: str, live: bool = True) -> Dict[str, Any]:
    """Run every check against a site. Returns a dict with score, grade, checks, bots and page facts."""
    url = normalise_url(target)
    base = origin(url)
    checks: List[Dict[str, Any]] = []

    # 1. baseline fetch as a browser
    home = fetch(url, accept="text/html,*/*")
    if home["status"] == 0:
        return {
            "url": url, "reachable": False, "error": home["error"], "score": 0, "grade": "F",
            "checks": [_check("reachable", "Site reachable", "fail", 0, 100, home["error"] or "no response",
                              "Make sure the host resolves and serves HTTPS.")],
            "bots": {}, "page": {}, "llms_txt": {}, "version": __version__,
        }
    final = home["final_url"]
    body = home["body"]
    base = origin(final)

    # HTTPS
    if final.startswith("https://"):
        checks.append(_check("https", "HTTPS", "pass", 5, 5, f"Served over HTTPS ({home['status']} in {home['ms']} ms)."))
    else:
        checks.append(_check("https", "HTTPS", "fail", 0, 5, f"Final URL is {final}.", "Serve the site over HTTPS and redirect HTTP to it."))

    # response time
    if home["ms"] > 4000:
        checks.append(_check("speed", "Response time", "warn", 1, 3, f"Homepage took {home['ms']} ms.", "AI crawlers give up on slow pages; get the HTML under 2 s."))
    else:
        checks.append(_check("speed", "Response time", "pass", 3, 3, f"Homepage HTML in {home['ms']} ms."))

    # 2. robots.txt and 3. live bot fetches, in parallel
    robots_resp = None
    live_results: Dict[str, Dict[str, Any]] = {}
    # Sites hosted on a sub-path (GitHub project pages, docs portals) can only publish llms.txt and a sitemap
    # under that path, so those two are also tried there. robots.txt is root-only by spec.
    sub = urllib.parse.urlsplit(final).path.rstrip("/")
    sub_base = base + sub if sub else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        f_robots = ex.submit(fetch, base + "/robots.txt", BROWSER_UA, "text/plain,*/*")
        f_llms = ex.submit(fetch, base + "/llms.txt", BROWSER_UA, "text/plain,*/*")
        f_llms_sub = ex.submit(fetch, sub_base + "/llms.txt", BROWSER_UA, "text/plain,*/*") if sub_base else None
        f_llms_full = ex.submit(fetch, base + "/llms-full.txt", BROWSER_UA, "text/plain,*/*", 200_000)
        f_live = {}
        if live:
            for name, _vendor, _role, ua in BOTS:
                if ua:
                    f_live[name] = ex.submit(fetch, final, ua, "text/html,*/*", 300_000)
        robots_resp = f_robots.result()
        llms_resp = f_llms.result()
        llms_full_resp = f_llms_full.result()
        llms_location = base + "/llms.txt"
        if f_llms_sub is not None and not validate_llms_txt(llms_resp)["present"]:
            alt = f_llms_sub.result()
            if validate_llms_txt(alt)["present"]:
                llms_resp, llms_location = alt, sub_base + "/llms.txt"
        for name, fut in f_live.items():
            live_results[name] = fut.result()

    robots = parse_robots(robots_resp["body"]) if robots_resp["status"] == 200 else {"groups": [], "sitemaps": []}
    robots_present = robots_resp["status"] == 200

    bots: Dict[str, Dict[str, Any]] = {}
    answer_blocked_robots: List[str] = []
    answer_blocked_edge: List[str] = []
    train_blocked: List[str] = []
    for name, vendor, role, ua in BOTS:
        verdict = robots_verdict(robots, name) if robots_present else "unspecified"
        entry: Dict[str, Any] = {"vendor": vendor, "role": role, "robots": verdict}
        lr = live_results.get(name)
        if lr is not None:
            entry["live_status"] = lr["status"]
            entry["live_blocked"] = (home["status"] == 200 and lr["status"] in (401, 403, 405, 406, 429, 503, 0))
            if lr["status"] == 0:
                entry["live_error"] = lr["error"]
        if verdict == "block":
            (answer_blocked_robots if role == "answer" else train_blocked).append(name)
        if entry.get("live_blocked") and role == "answer":
            answer_blocked_edge.append(name)
        bots[name] = entry
    train_blocked_edge = [n for n, e in bots.items() if e["role"] == "train" and e.get("live_blocked")]

    if not robots_present:
        checks.append(_check("robots", "robots.txt allows answer engines", "pass", 15, 15,
                             f"No robots.txt ({robots_resp['status']}), so every crawler is allowed by default."))
    elif answer_blocked_robots:
        checks.append(_check("robots", "robots.txt allows answer engines", "fail", 0, 15,
                             "robots.txt blocks: " + ", ".join(answer_blocked_robots) + ".",
                             "These bots fetch pages to answer live questions and cite you. Remove the Disallow for them "
                             "(you can still block training bots like GPTBot and ClaudeBot separately)."))
    else:
        checks.append(_check("robots", "robots.txt allows answer engines", "pass", 15, 15,
                             "OAI-SearchBot, ChatGPT-User, Claude-SearchBot, Claude-User, PerplexityBot, Bingbot and Googlebot are allowed."))

    if live:
        if answer_blocked_edge:
            det = ", ".join(f"{n} got HTTP {bots[n]['live_status']}" for n in answer_blocked_edge)
            also_robots = all(n in answer_blocked_robots for n in answer_blocked_edge)
            checks.append(_check("edge", "Answer engines not blocked at the edge", "fail", 0, 20,
                                 f"Fetching the homepage as a bot: {det}; a browser got {home['status']}.",
                                 ("Your CDN or WAF (Cloudflare 'Block AI bots', bot-fight mode, a rate limiter) is refusing them"
                                  + (" on top of the robots.txt block." if also_robots else " even though robots.txt allows them.")
                                  + " Add these user agents to the allow list.")))
        else:
            checks.append(_check("edge", "Answer engines not blocked at the edge", "pass", 20, 20,
                                 "Homepage returned the same status to OAI-SearchBot, ChatGPT-User, Claude-SearchBot, Claude-User, "
                                 "PerplexityBot and Perplexity-User as to a browser."))
    if train_blocked or train_blocked_edge:
        checks.append(_check("training", "Training crawlers", "info", 0, 0,
                             "Blocked (your choice, no penalty): " + ", ".join(sorted(set(train_blocked + train_blocked_edge))) + "."))
    else:
        checks.append(_check("training", "Training crawlers", "info", 0, 0,
                             "GPTBot, ClaudeBot, Google-Extended, CCBot and friends are allowed. Block them in robots.txt if you do not want to be training data; it does not affect citations."))

    # 4. llms.txt
    llms = validate_llms_txt(llms_resp)
    llms["full_present"] = llms_full_resp["status"] == 200 and llms_full_resp["body"].lstrip()[:1] != "<"
    if llms["present"]:
        quality = sum([llms["has_h1"], llms["has_summary"], llms["link_count"] > 0])
        pts = {3: 15, 2: 12, 1: 8, 0: 5}[quality]
        missing = [x for x, ok in (("an H1 title", llms["has_h1"]), ("a '> summary' line", llms["has_summary"]), ("markdown links to key pages", llms["link_count"] > 0)) if not ok]
        llms["url"] = llms_location
        checks.append(_check("llms_txt", "/llms.txt", "pass" if quality == 3 else "warn", pts, 15,
                             f"Present at {llms_location} ({llms['bytes']} bytes, {llms['link_count']} links" + (", llms-full.txt too" if llms["full_present"] else "") + ").",
                             ("Add " + " and ".join(missing) + ". Run `citable " + target + " --llms-txt` for a draft.") if missing else ""))
    else:
        why = "returns HTML (a soft 404)" if llms["soft_404"] else f"returns HTTP {llms['status']}"
        checks.append(_check("llms_txt", "/llms.txt", "fail", 0, 15, f"/llms.txt {why}.",
                             f"Publish a plain-text /llms.txt: an H1, a one-line '> summary', then links to your key pages. "
                             f"`citable {target} --llms-txt` prints a draft from your homepage."))

    # 5. homepage facts
    title = _title(body)
    desc = _meta(body, "description")
    canonical = _canonical(body)
    lang = _lang(body)
    robots_meta = (_meta(body, "robots") or "").lower()
    og_title = _meta(body, "og:title")
    og_desc = _meta(body, "og:description")
    ld = _jsonld(body)
    ld_types = _jsonld_types(ld)
    text = _strip_html(body)
    words = len(text.split())
    scripts = len(re.findall(r"(?i)<script\b", body))
    h1s = len(re.findall(r"(?i)<h1\b", body))

    if "noindex" in robots_meta or "noindex" in home["headers"].get("x-robots-tag", "").lower():
        checks.append(_check("noindex", "No noindex", "fail", 0, 10, "The homepage carries a noindex directive.", "Remove noindex from the meta robots tag or X-Robots-Tag header."))
    else:
        checks.append(_check("noindex", "No noindex", "pass", 2, 2, "No noindex directive on the homepage."))

    if title and desc and 10 <= len(title) <= 70 and 50 <= len(desc) <= 200:
        checks.append(_check("meta", "Title and description", "pass", 8, 8, f"Title: {title!r} ({len(title)} chars); description {len(desc)} chars."))
    elif title and desc:
        checks.append(_check("meta", "Title and description", "warn", 5, 8, f"Title {len(title)} chars, description {len(desc)} chars.",
                             "Keep the title under 70 characters and the description between 50 and 200: answer engines quote these verbatim."))
    else:
        checks.append(_check("meta", "Title and description", "fail", 0, 8,
                             ("Missing <title>. " if not title else "") + ("Missing meta description." if not desc else ""),
                             "Add a plain-language <title> and <meta name=description>: they are the first thing an answer engine reads."))

    if canonical:
        checks.append(_check("canonical", "Canonical URL", "pass", 4, 4, f"Canonical: {canonical}"))
    else:
        checks.append(_check("canonical", "Canonical URL", "warn", 1, 4, "No <link rel=canonical>.", "Add a canonical link so citations point at one URL, not five variants."))

    if ld_types:
        rich = any(t in ld_types for t in ("Organization", "WebSite", "Article", "FAQPage", "Product", "SoftwareApplication", "Person", "LocalBusiness", "HowTo", "BlogPosting", "NewsArticle"))
        checks.append(_check("schema", "Structured data (JSON-LD)", "pass" if rich else "warn", 10 if rich else 6, 10,
                             "Types: " + ", ".join(sorted(set(ld_types))) + ".",
                             "" if rich else "Add Organization or WebSite schema with name, url and description."))
    else:
        checks.append(_check("schema", "Structured data (JSON-LD)", "fail", 0, 10, "No JSON-LD found.",
                             "Add a JSON-LD block with @type Organization (or WebSite), name, url, description and sameAs. "
                             "FAQPage blocks get quoted directly in answers."))

    if words >= 250:
        checks.append(_check("content", "Content readable without JavaScript", "pass", 15, 15, f"{words} words of visible text in the raw HTML, {h1s} h1."))
    elif words >= 80:
        checks.append(_check("content", "Content readable without JavaScript", "warn", 7, 15, f"Only {words} words in the raw HTML ({scripts} scripts).",
                             "Most AI crawlers do not run JavaScript. Server-render or prerender the main copy."))
    else:
        checks.append(_check("content", "Content readable without JavaScript", "fail", 0, 15,
                             f"Only {words} words in the raw HTML with {scripts} script tags: this looks like a client-rendered shell.",
                             "AI crawlers (and citations) only see server-rendered HTML. Prerender or SSR the pages you want cited."))

    if og_title or og_desc:
        checks.append(_check("og", "Open Graph", "pass", 3, 3, "og:title/og:description present."))
    else:
        checks.append(_check("og", "Open Graph", "warn", 1, 3, "No Open Graph tags.", "Add og:title and og:description; several engines use them as the snippet."))

    # 6. sitemap
    sitemap_url = robots["sitemaps"][0] if robots.get("sitemaps") else base + "/sitemap.xml"
    sm = fetch(sitemap_url, BROWSER_UA, "application/xml,text/xml,*/*", 200_000)
    sm_ok = sm["status"] == 200 and ("<urlset" in sm["body"] or "<sitemapindex" in sm["body"])
    if not sm_ok and sub_base and not robots.get("sitemaps"):
        alt_sm = fetch(sub_base + "/sitemap.xml", BROWSER_UA, "application/xml,text/xml,*/*", 200_000)
        if alt_sm["status"] == 200 and ("<urlset" in alt_sm["body"] or "<sitemapindex" in alt_sm["body"]):
            sitemap_url, sm, sm_ok = sub_base + "/sitemap.xml", alt_sm, True
    if sm_ok:
        checks.append(_check("sitemap", "Sitemap", "pass", 5, 5, f"{sitemap_url} is a valid sitemap" + (" (listed in robots.txt)." if robots.get("sitemaps") else " (default path; not listed in robots.txt).")))
    else:
        checks.append(_check("sitemap", "Sitemap", "warn", 0, 5, f"{sitemap_url} returned {sm['status']}" + ("" if sm["status"] != 200 else " but is not XML") + ".",
                             "Publish /sitemap.xml and add a `Sitemap:` line to robots.txt."))

    if lang:
        checks.append(_check("lang", "Language declared", "pass", 2, 2, f"<html lang={lang}>."))
    else:
        checks.append(_check("lang", "Language declared", "warn", 0, 2, "No lang attribute on <html>.", "Add <html lang=\"en\"> (or your language)."))

    max_total = sum(c["max"] for c in checks)
    got = sum(c["points"] for c in checks)
    score = int(round(100 * got / max_total)) if max_total else 0
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"

    return {
        "url": url, "final_url": final, "reachable": True, "score": score, "grade": grade,
        "checks": checks, "bots": bots,
        "page": {"title": title, "description": desc, "canonical": canonical, "lang": lang, "words": words,
                 "jsonld_types": sorted(set(ld_types)), "h1_count": h1s, "status": home["status"], "ms": home["ms"],
                 "last_modified": home["headers"].get("last-modified")},
        "llms_txt": llms, "robots_txt_present": robots_present, "sitemap": sitemap_url if sm_ok else None,
        "version": __version__, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def draft_llms_txt(target: str, max_links: int = 25) -> str:
    """Build a starter llms.txt from the homepage: H1 title, '> summary', and same-site links with anchor text."""
    url = normalise_url(target)
    home = fetch(url, accept="text/html,*/*")
    if home["status"] == 0:
        return f"# {urllib.parse.urlsplit(url).netloc}\n\n> (could not fetch the homepage: {home['error']})\n"
    body, final = home["body"], home["final_url"]
    host = urllib.parse.urlsplit(final).netloc
    ld = _jsonld(body)
    name = None
    for it in ld:
        if it.get("@type") in ("Organization", "WebSite") and it.get("name"):
            name = str(it["name"])
            break
    name = name or _meta(body, "og:site_name") or (_title(body) or host).split(" | ")[0].split(" - ")[0].strip() or host
    summary = _meta(body, "description") or _meta(body, "og:description") or f"{name} ({host})."
    links = _links(body, final)
    lines = [f"# {name}", "", f"> {summary}", "",
             f"This file follows the llms.txt convention (https://llmstxt.org). Site: {final}", "", "## Key pages", ""]
    if not links:
        lines.append(f"- [{name}]({final}): homepage")
    for u, text in links[:max_links]:
        label = text or u.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ") or u
        lines.append(f"- [{label}]({u})")
    lines += ["", "## Optional", "", "- [Sitemap](" + origin(final) + "/sitemap.xml)", ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- report

def format_report(r: Dict[str, Any]) -> str:
    out = [f"citable {__version__}: {r['url']}"]
    if not r.get("reachable"):
        out.append(f"  unreachable: {r.get('error')}")
        return "\n".join(out)
    out.append(f"  score {r['score']}/100  grade {r['grade']}   (final URL {r['final_url']})")
    out.append("")
    marks = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
    for c in r["checks"]:
        pts = f"{c['points']:>2}/{c['max']:<2}" if c["max"] else "     "
        out.append(f"  [{marks[c['status']]}] {pts} {c['title']}: {c['detail']}")
        if c["fix"] and c["status"] in ("warn", "fail"):
            out.append(f"         fix: {c['fix']}")
    out.append("")
    out.append("  Bot                 Vendor                 Role    robots.txt   live fetch")
    for name, e in r["bots"].items():
        live = ""
        if "live_status" in e:
            live = f"HTTP {e['live_status']}" if e["live_status"] else f"error ({e.get('live_error', '')[:40]})"
            if e.get("live_blocked"):
                live += "  <- BLOCKED"
        out.append(f"  {name:<19} {e['vendor']:<22} {e['role']:<7} {e['robots']:<12} {live}")
    out.append("")
    out.append("  Watch this site weekly and get an email when a bot gets blocked or llms.txt breaks: https://github.com/genzagents/citable#watch")
    return "\n".join(out)


# ----------------------------------------------------------------------------- MCP server (stdio)

MCP_TOOLS = [
    {
        "name": "check_site",
        "description": "Check whether AI answer engines (ChatGPT search, Claude, Perplexity) can read and cite a website. "
                       "Fetches robots.txt, llms.txt, sitemap and the homepage as each AI bot, scores 0-100 and lists fixes.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Site URL or domain, e.g. example.com"},
            "live": {"type": "boolean", "description": "Also fetch the homepage as each AI bot to detect WAF/CDN blocks (default true)", "default": True},
        }, "required": ["url"]},
    },
    {
        "name": "draft_llms_txt",
        "description": "Draft a starter /llms.txt for a website from its homepage: title, summary and links to key pages.",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "description": "Site URL or domain"}}, "required": ["url"]},
    },
]


def _mcp_send(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def mcp_serve() -> None:
    """Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0). No dependencies."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _mcp_send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if mid is None:  # notification
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "citable", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if name == "check_site":
                    r = check_site(args["url"], live=bool(args.get("live", True)))
                    result = {"content": [{"type": "text", "text": format_report(r)}], "structuredContent": r, "isError": False}
                elif name == "draft_llms_txt":
                    result = {"content": [{"type": "text", "text": draft_llms_txt(args["url"])}], "isError": False}
                else:
                    raise KeyError(f"unknown tool {name}")
            else:
                _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            _mcp_send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:
            _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})


# ----------------------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    if argv[0] == "mcp":
        mcp_serve()
        return 0
    if argv[0] in ("watch", "track", "hosted"):
        import importlib
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # bare-script runs
        return importlib.import_module("citable." + argv[0]).main(argv[1:])
    target = argv[0]
    flags = set(argv[1:])
    if "--llms-txt" in flags:
        print(draft_llms_txt(target), end="")
        return 0
    r = check_site(target, live="--no-live" not in flags)
    if "--json" in flags:
        print(json.dumps(r, indent=2))
    else:
        print(format_report(r))
    if not r.get("reachable"):
        return 2
    return 0 if r["score"] >= int(dict(f.split("=", 1) for f in flags if f.startswith("--min-score=")).get("--min-score", 0)) else 1


if __name__ == "__main__":
    sys.exit(main())
