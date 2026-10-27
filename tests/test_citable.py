import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from citable import (_jsonld, _jsonld_types, _links, _meta, _strip_html, _title, normalise_url,  # noqa: E402
                     parse_robots, robots_verdict, validate_llms_txt)

ROBOTS = """
# comment
User-agent: *
Disallow: /admin
Allow: /admin/public
Sitemap: https://example.com/sitemap.xml

User-agent: GPTBot
User-agent: ClaudeBot
Disallow: /

User-agent: PerplexityBot
Disallow:

User-agent: OAI-SearchBot
Allow: /blog/
Disallow: /blog/*.pdf$
"""


class RobotsTests(unittest.TestCase):
    def setUp(self):
        self.r = parse_robots(ROBOTS)

    def test_groups_and_sitemap(self):
        self.assertEqual(len(self.r["groups"]), 4)
        self.assertEqual(self.r["groups"][1]["agents"], ["gptbot", "claudebot"])
        self.assertEqual(self.r["sitemaps"], ["https://example.com/sitemap.xml"])

    def test_wildcard_group(self):
        self.assertEqual(robots_verdict(self.r, "Bingbot", "/"), "allow")
        self.assertEqual(robots_verdict(self.r, "Bingbot", "/admin/secret"), "block")
        self.assertEqual(robots_verdict(self.r, "Bingbot", "/admin/public/x"), "allow")  # longest match wins

    def test_specific_group_blocks_root(self):
        self.assertEqual(robots_verdict(self.r, "GPTBot"), "block")
        self.assertEqual(robots_verdict(self.r, "claudebot"), "block")

    def test_empty_disallow_means_allow(self):
        self.assertEqual(robots_verdict(self.r, "PerplexityBot"), "allow")

    def test_patterns(self):
        self.assertEqual(robots_verdict(self.r, "OAI-SearchBot", "/blog/post"), "allow")
        self.assertEqual(robots_verdict(self.r, "OAI-SearchBot", "/blog/file.pdf"), "block")
        self.assertEqual(robots_verdict(self.r, "OAI-SearchBot", "/blog/file.pdf?x=1"), "allow")  # $ anchors

    def test_no_robots(self):
        self.assertEqual(robots_verdict({"groups": [], "sitemaps": []}, "GPTBot"), "unspecified")

    def test_specific_group_without_matching_rule_allows(self):
        r = parse_robots("User-agent: GPTBot\nDisallow: /private\n")
        self.assertEqual(robots_verdict(r, "GPTBot", "/"), "allow")
        self.assertEqual(robots_verdict(r, "GPTBot", "/private/x"), "block")


HTML = """<!doctype html><html lang="en-GB"><head>
<title> Acme &amp; Co | Widgets </title>
<meta name="description" content="We make widgets.">
<meta property="og:title" content="Acme">
<link rel="canonical" href="https://acme.test/">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization","name":"Acme"},{"@type":["WebSite","Thing"]}]}</script>
<script>var x = "<p>not content</p>";</script>
<style>p{color:red}</style>
</head><body><h1>Acme</h1><p>Hello <b>world</b> &nbsp; again</p>
<a href="/about">About us</a> <a href="/about/">About dup</a> <a href="https://other.test/x">ext</a>
<a href="/logo.png">img</a> <a href="mailto:a@b">mail</a> <a href="/">home</a>
</body></html>"""


class HtmlTests(unittest.TestCase):
    def test_title_meta_canonical(self):
        self.assertEqual(_title(HTML), "Acme & Co | Widgets")
        self.assertEqual(_meta(HTML, "description"), "We make widgets.")
        self.assertEqual(_meta(HTML, "og:title"), "Acme")
        self.assertIsNone(_meta(HTML, "og:description"))
        self.assertEqual(_jsonld_types(_jsonld(HTML)), ["Organization", "WebSite", "Thing"])

    def test_strip_html_ignores_scripts(self):
        text = _strip_html(HTML)
        self.assertIn("Hello world", text)
        self.assertNotIn("not content", text)
        self.assertNotIn("color:red", text)

    def test_links_same_origin_deduped(self):
        links = _links(HTML, "https://acme.test/")
        self.assertEqual(links, [("https://acme.test/about", "About us")])


class LlmsTxtTests(unittest.TestCase):
    def _resp(self, status, body, ctype="text/plain"):
        return {"status": status, "body": body, "headers": {"content-type": ctype}}

    def test_good_file(self):
        v = validate_llms_txt(self._resp(200, "# Acme\n\n> Widgets.\n\n- [Docs](https://acme.test/docs)\n"))
        self.assertTrue(v["present"] and v["has_h1"] and v["has_summary"])
        self.assertEqual(v["link_count"], 1)

    def test_soft_404(self):
        v = validate_llms_txt(self._resp(200, "<!doctype html><html>404</html>", "text/html"))
        self.assertFalse(v["present"])
        self.assertTrue(v["soft_404"])

    def test_missing(self):
        v = validate_llms_txt(self._resp(404, "Not found"))
        self.assertFalse(v["present"])


class UrlTests(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(normalise_url("Example.com"), "https://example.com/")
        self.assertEqual(normalise_url("http://x.test/a?b=1#frag"), "http://x.test/a?b=1")


class McpSchemaTests(unittest.TestCase):
    def test_tools_are_valid_json_schema_shapes(self):
        from citable import MCP_TOOLS
        names = {t["name"] for t in MCP_TOOLS}
        self.assertEqual(names, {"check_site", "draft_llms_txt"})
        for t in MCP_TOOLS:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn("url", t["inputSchema"]["required"])
            json.dumps(t)


class FindIssueTests(unittest.TestCase):
    """find_issue reads the label listing first (search lags new issues) and picks the oldest duplicate."""

    def _run(self, by_label, by_search):
        from citable import watch
        calls = []

        def fake_gh(*args, input_=None):
            calls.append(args)
            return json.dumps(by_label if "--label" in args else by_search)

        orig, watch.gh = watch.gh, fake_gh
        try:
            return watch.find_issue("o/r", "citable watch: x", "citable"), calls
        finally:
            watch.gh = orig

    def test_oldest_duplicate_from_label_listing(self):
        dup = [{"number": n, "title": "citable watch: x", "body": "", "state": "OPEN"} for n in (2, 1)]
        it, calls = self._run(dup, [])
        self.assertEqual(it["number"], 1)
        self.assertEqual(len(calls), 1)

    def test_falls_back_to_search_when_label_removed(self):
        it, calls = self._run([], [{"number": 7, "title": "citable watch: X", "body": "", "state": "CLOSED"}])
        self.assertEqual(it["number"], 7)
        self.assertEqual(len(calls), 2)

    def test_none_when_absent(self):
        it, _ = self._run([{"number": 3, "title": "other", "body": "", "state": "OPEN"}], [])
        self.assertIsNone(it)


if __name__ == "__main__":
    unittest.main()


class WatchTests(unittest.TestCase):
    def _report(self, score=95, llms=True, edge_block=(), robots_block=(), failing=(), reachable=True):
        bots = {}
        for b in ("OAI-SearchBot", "ChatGPT-User", "PerplexityBot"):
            bots[b] = {"vendor": "x", "role": "answer", "robots": "block" if b in robots_block else "allow",
                       "live_status": 403 if b in edge_block else 200, "live_blocked": b in edge_block}
        bots["GPTBot"] = {"vendor": "x", "role": "train", "robots": "block", "live_status": 200, "live_blocked": False}
        checks = [{"id": i, "title": t, "status": "fail" if i in failing else "pass", "points": 0, "max": 5,
                   "detail": "d", "fix": "f"} for i, t in (("https", "HTTPS"), ("jsonld", "JSON-LD"), ("sitemap", "Sitemap"))]
        return {"url": "https://example.com/", "final_url": "https://example.com/", "reachable": reachable,
                "score": score, "grade": "A", "checks": checks, "bots": bots,
                "llms_txt": {"present": llms}, "checked_at": "2026-10-26T00:00:00Z"}

    def test_snapshot_only_keeps_answer_bots(self):
        from citable.watch import snapshot
        s = snapshot(self._report(edge_block=("ChatGPT-User",), robots_block=("PerplexityBot",), failing=("jsonld",)))
        self.assertEqual(s["bots"], {"OAI-SearchBot": "ok", "ChatGPT-User": "edge", "PerplexityBot": "robots"})
        self.assertEqual(s["failing"], ["jsonld"])
        self.assertTrue(s["llms_txt"])

    def test_diff_reports_regressions_and_improvements(self):
        from citable.watch import diff, snapshot
        prev = snapshot(self._report(score=95, failing=("sitemap",)))
        cur = snapshot(self._report(score=70, llms=False, edge_block=("ChatGPT-User",), failing=("jsonld",)))
        worse, better = diff(prev, cur)
        self.assertTrue(any("ChatGPT-User is now blocked at the edge" in w for w in worse))
        self.assertTrue(any("/llms.txt is gone" in w for w in worse))
        self.assertTrue(any("Now failing: JSON-LD" in w for w in worse))
        self.assertTrue(any("Score fell from 95 to 70" in w for w in worse))
        self.assertEqual(better, ["Fixed: Sitemap."])
        self.assertEqual(diff(None, cur), ([], []))
        self.assertEqual(diff(cur, cur), ([], []))

    def test_unreachable_is_one_regression(self):
        from citable.watch import diff, snapshot
        prev = snapshot(self._report())
        cur = snapshot({"url": "https://example.com/", "reachable": False, "score": 0, "grade": "F", "checks": [], "bots": {}, "llms_txt": {}})
        worse, better = diff(prev, cur)
        self.assertEqual(len(worse), 1)
        self.assertIn("unreachable", worse[0])

    def test_state_round_trips_through_issue_body(self):
        from citable.watch import host_of, issue_title, parse_state, render_body, snapshot
        r = self._report()
        snap = snapshot(r)
        body = render_body(r, snap, "https://github.com/o/r/actions/runs/1")
        self.assertEqual(parse_state(body), json.loads(json.dumps(snap)))
        self.assertIn("| ChatGPT-User | allow | HTTP 200 |", body)
        self.assertEqual(issue_title("https://Example.com/"), "citable watch: example.com")
        self.assertEqual(host_of("docs.example.com/guide/"), "docs.example.com/guide")
        self.assertIsNone(parse_state("no state here"))


class TrackTests(unittest.TestCase):
    def test_judge_cited_vs_mentioned(self):
        from citable.track import judge
        j = judge("example.com", "Try Example (example.com) or https://rival.io/tools.", ["https://www.example.com/guide"], brand="Example")
        self.assertTrue(j["cited"]); self.assertTrue(j["mentioned"])
        self.assertEqual(j["our_urls"], ["https://www.example.com/guide"])
        self.assertEqual(j["other_hosts"], [("rival.io", 1)])
        j = judge("example.com", "Nothing about it here. See https://rival.io/ and https://other.org/x.", [])
        self.assertFalse(j["cited"]); self.assertFalse(j["mentioned"])
        j = judge("example.com", "Example is good.", [], brand="Example")
        self.assertFalse(j["cited"]); self.assertTrue(j["mentioned"])
        self.assertTrue(judge("example.com", "", ["https://docs.example.com/a"])["cited"])
        self.assertFalse(judge("example.com", "", ["https://notexample.com/a"])["cited"])

    def test_parsers(self):
        from citable.track import parse_anthropic, parse_openai, parse_perplexity
        a = {"content": [{"type": "server_tool_use", "name": "web_search"},
                         {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://seen.com/"}]},
                         {"type": "text", "text": "Use A.", "citations": [{"type": "web_search_result_location", "url": "https://a.com/x"}]},
                         {"type": "text", "text": " Or B."}]}
        self.assertEqual(parse_anthropic(a), ("Use A.\n Or B.", ["https://a.com/x"]))
        o = {"output": [{"type": "web_search_call"}, {"type": "message", "content": [
            {"type": "output_text", "text": "Try A", "annotations": [{"type": "url_citation", "url": "https://a.com/"}, {"type": "file_citation"}]}]}]}
        self.assertEqual(parse_openai(o), ("Try A", ["https://a.com/"]))
        p = {"choices": [{"message": {"content": "A then B"}}], "citations": ["https://a.com/", "https://b.com/"],
             "search_results": [{"url": "https://b.com/"}, {"url": "https://c.com/"}]}
        self.assertEqual(parse_perplexity(p), ("A then B", ["https://a.com/", "https://b.com/", "https://c.com/"]))
        self.assertEqual(parse_perplexity({}), ("", []))

    def test_snapshot_and_diff(self):
        from citable.track import diff, snapshot
        res1 = [{"engine": "openai", "prompt": "best x", "cited": True}, {"engine": "openai", "prompt": "how y", "cited": False},
                {"engine": "perplexity", "prompt": "best x", "cited": True, "error": "HTTP 401"}]
        s1 = snapshot("www.Example.com", res1)
        self.assertEqual(s1["domain"], "example.com")
        self.assertEqual(s1["visibility"], 50)
        self.assertEqual(s1["pairs"]["perplexity|best x"], "error")
        res2 = [{"engine": "openai", "prompt": "best x", "cited": False}, {"engine": "openai", "prompt": "how y", "cited": True},
                {"engine": "perplexity", "prompt": "best x", "cited": True}]
        worse, better = diff(s1, snapshot("example.com", res2))
        self.assertEqual(worse, ['openai no longer cites you for "best x".'])
        self.assertEqual(better, ['openai now cites you for "how y".', "Visibility rose from 50% to 67%."])
        self.assertEqual(diff(None, s1), ([], []))

    def test_markdown_and_state_round_trip(self):
        from citable.track import render_body, render_markdown, snapshot
        from citable.watch import parse_state
        t = {"domain": "example.com", "prompts": ["best x"], "engines": ["openai"], "visibility": 100, "competitors": [("rival.io", 2)],
             "results": [{"engine": "openai", "prompt": "best x", "cited": True, "mentioned": True, "our_urls": [], "other_hosts": [], "excerpt": ""}]}
        md = render_markdown(t)
        self.assertIn("| best x | **cited** |", md)
        self.assertIn("rival.io (2)", md)
        snap = snapshot("example.com", t["results"])
        self.assertEqual(parse_state(render_body(t, snap, "")), json.loads(json.dumps(snap)))


class HostedTests(unittest.TestCase):
    """The paid tier: Polar subscriptions in, emails out, state in between. No network: check_site is faked."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        os.environ["CITABLE_STATE_DIR"] = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CITABLE_STATE_DIR", None)

    @staticmethod
    def _sub(**custom):
        data = {"site": "Example.com\nhttps://docs.example.com/", "prompts": "best widgets\nwidget pricing", "brand": "Example"}
        data.update(custom)
        return {"id": "sub_1", "status": "active", "current_period_end": "2026-11-26T00:00:00Z",
                "customer": {"email": "pat@example.com", "name": "Pat"}, "custom_field_data": data}

    @staticmethod
    def _report(score, reachable=True, blocked=()):
        bots = {"ChatGPT-User": {"vendor": "OpenAI", "role": "answer", "robots": "allow", "live_status": 403 if "ChatGPT-User" in blocked else 200,
                                 "live_blocked": "ChatGPT-User" in blocked},
                "GPTBot": {"vendor": "OpenAI", "role": "training", "robots": "allow"}}
        checks = [{"id": "llms_txt", "title": "llms.txt", "status": "pass", "points": 10, "max": 10, "detail": "present", "fix": ""}]
        return {"url": "https://example.com/", "final_url": "https://example.com/", "score": score, "grade": "A" if score >= 90 else "C",
                "reachable": reachable, "bots": bots, "llms_txt": {"present": True}, "checks": checks, "checked_at": "2026-10-26T17:00:00Z"}

    def test_normalise_polar_subscription(self):
        from citable.hosted import normalise
        s = normalise(self._sub())
        self.assertEqual(s["sites"], ["example.com", "docs.example.com"])
        self.assertEqual(s["prompts"], ["best widgets", "widget pricing"])
        self.assertEqual((s["email"], s["name"], s["brand"]), ("pat@example.com", "Pat", "Example"))
        self.assertEqual(normalise(self._sub(), max_sites=1)["sites"], ["example.com"])
        self.assertIsNone(normalise({**self._sub(), "status": "canceled"}))
        self.assertIsNone(normalise(self._sub(site="")))
        self.assertEqual(normalise(self._sub(site=None, Domains="a.io, b.io"))["sites"], ["a.io", "b.io"])

    def test_decide(self):
        from citable.hosted import WEEK, decide
        now = 1_000_000.0
        self.assertEqual(decide(None, [], [], now), "first")
        self.assertEqual(decide({"last_report_at": now - 60}, ["x"], [], now), "alert")
        self.assertEqual(decide({"last_report_at": now - 60}, [], ["y"], now), "alert")
        self.assertEqual(decide({"last_report_at": now - 60}, [], [], now), "")
        self.assertEqual(decide({"last_report_at": now - WEEK}, [], [], now), "digest")
        self.assertEqual(decide({"last_report_at": now - 60}, [], [], now, tracked=True), "digest")

    def test_run_first_then_quiet_then_alert_then_digest(self):
        import io
        from citable import hosted
        from citable.hosted import WEEK, load_state, normalise, run
        calls = []
        def fake_check(site, live=True):
            calls.append(site)
            return self._report(95) if "docs" not in site else self._report(80)
        hosted.check_site = fake_check
        hosted.T.available_engines = lambda: []  # no engines: tracking is skipped quietly
        sub = normalise(self._sub())
        now = 2_000_000.0
        out = io.StringIO()
        run([sub], dry_run=True, now=now, out=out)
        self.assertIn("would send [first] to pat@example.com: Your first citable report: example.com, docs.example.com", out.getvalue())
        self.assertIn("Hi Pat,", out.getvalue())
        self.assertEqual(sorted(calls), ["docs.example.com", "example.com"])
        st = load_state("sub_1")
        self.assertEqual(st["last_kind"], "first")
        self.assertEqual(st["checks"]["example.com"]["score"], 95)
        # an hour later, nothing changed: silence
        out = io.StringIO()
        run([sub], dry_run=True, now=now + 3600, out=out)
        self.assertIn("no change", out.getvalue())
        self.assertNotIn("would send", out.getvalue())
        # a bot gets edge-blocked on one site: alert names the site
        hosted.check_site = lambda site, live=True: self._report(95, blocked=("ChatGPT-User",))
        out = io.StringIO()
        run([sub], dry_run=True, now=now + 7200, out=out)
        self.assertIn("would send [alert] to pat@example.com: citable alert: example.com: ", out.getvalue())
        self.assertIn("WHAT GOT WORSE", out.getvalue())
        # a week with no change: digest with the scores in the subject
        out = io.StringIO()
        run([sub], dry_run=True, now=now + 7200 + WEEK, out=out)
        self.assertIn("would send [digest] to pat@example.com: citable weekly: example.com, docs.example.com (95 / 95)", out.getvalue())

    def test_render_email_escapes_html(self):
        from citable.hosted import render_email
        sub = {"email": "a@b.c", "name": "", "sites": ["x.io"], "prompts": [], "brand": ""}
        text, html = render_email("digest", sub, [self._report(50)], ["x.io: <b>bad</b>"], [], [])
        self.assertIn("Hi there,", text)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", html)
        self.assertNotIn("<b>bad</b>", html)
