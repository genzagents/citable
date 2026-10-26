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
