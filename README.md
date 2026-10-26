# citable

Can ChatGPT, Claude and Perplexity actually read and cite your site?

Most "AI visibility" checkers read `robots.txt` and stop. `citable` fetches your homepage **as each AI bot**
and reports the blocks that `robots.txt` never mentions (Cloudflare "Block AI bots", WAF rules, rate limiters),
then checks the rest of the list: `llms.txt`, JSON-LD, title and description, JS-only content, sitemap, canonical.
Score out of 100, every failure with the fix. Also runs as an **MCP server** so your agent can check sites for you.

Zero dependencies. One file. Python 3.9+.

```bash
pipx install git+https://github.com/genzagents/citable     # or: uvx --from git+https://github.com/genzagents/citable citable
citable example.com                # report
citable example.com --json         # for scripts and CI (exit 1 if below --min-score=N)
citable example.com --llms-txt     # a starter llms.txt built from your homepage
citable watch a.com b.com          # weekly watch: GitHub issue per site, comment (email) on regressions
citable track a.com "best X" "how Y"  # prompt tracking: which engines cite you, who gets cited instead
```

No install at all:

```bash
curl -sL https://raw.githubusercontent.com/genzagents/citable/main/citable/__init__.py | python3 - example.com
```

## What a run looks like

```
citable 0.3.0: https://reddit.com/
  score 13/100  grade F   (final URL https://www.reddit.com/)

  [FAIL]  0/15 robots.txt allows answer engines: robots.txt blocks: OAI-SearchBot, ChatGPT-User, Claude-SearchBot, ...
  [FAIL]  0/20 Answer engines not blocked at the edge: Fetching the homepage as a bot: ChatGPT-User got HTTP 403; a browser got 200.
         fix: Your CDN or WAF (Cloudflare 'Block AI bots', bot-fight mode, a rate limiter) is refusing them ...
  [FAIL]  0/15 /llms.txt: /llms.txt returns HTTP 404.
  ...
  Bot                 Vendor                 Role    robots.txt   live fetch
  OAI-SearchBot       OpenAI                 answer  block        HTTP 200
  ChatGPT-User        OpenAI                 answer  block        HTTP 403  <- BLOCKED
  Claude-SearchBot    Anthropic              answer  block        HTTP 200
  ...
```

## What it checks

| Check | Points | Why it matters |
|---|---|---|
| robots.txt allows the **answer** bots (OAI-SearchBot, ChatGPT-User, Claude-SearchBot, Claude-User, PerplexityBot, Perplexity-User, DuckAssistBot, Bingbot, Googlebot) | 15 | These fetch pages to answer live questions and cite the source. Blocked means never cited. |
| Not blocked at the edge: homepage fetched **as** each of those bots returns what a browser gets | 20 | CDN and WAF "block AI" toggles stop bots that robots.txt allows. Nothing else on this list matters until this passes. |
| Training crawlers (GPTBot, ClaudeBot, Google-Extended, CCBot, Applebot-Extended, meta-externalagent, Amazonbot, Bytespider) | info | Blocking them is a legitimate choice and does not affect citations. Reported, never penalised. |
| `/llms.txt` present, plain text, with an H1, a `> summary` and links | 15 | The convention answer engines and agents read first. A soft-404 HTML page counts as missing. |
| Content readable without JavaScript (words in the raw HTML) | 15 | Most AI crawlers do not run JS. A client-rendered shell is invisible to them. |
| JSON-LD structured data (Organization, WebSite, Article, FAQPage...) | 10 | Entities and FAQs get quoted directly. |
| Title and meta description, sensible lengths | 8 | Quoted verbatim in answers. |
| HTTPS, response time, canonical, Open Graph, sitemap, `<html lang>`, no `noindex` | 22 | Hygiene that decides which URL gets the citation. |

## As an MCP server (Claude Code, Claude Desktop, Cursor, any MCP client)

Tools: `check_site(url, live=true)` and `draft_llms_txt(url)`.

Claude Code plugin (includes the MCP server and a skill that knows how to act on the report):

```bash
claude plugin marketplace add genzagents/marketplace
claude plugin install citable@genzagents
```

Any MCP client, by hand:

```json
{ "mcpServers": { "citable": { "command": "python3", "args": ["/path/to/citable/citable/__init__.py", "mcp"] } } }
```

The server speaks MCP over stdio with no dependencies, so it runs anywhere Python does.

## In CI

Fail the build when a deploy breaks AI access (a new WAF rule, a lost `llms.txt`, a `Disallow: /` that slipped in):

```bash
citable yoursite.com --min-score=70 --json > citable.json
```

## Watch

A deploy that flips a WAF toggle silently removes you from every AI answer. The watch is the alarm. It runs as a
GitHub Action in any repo you own (it need not be the site's repo), keeps one issue per site up to date, and
**comments on that issue the moment an answer bot is blocked, `llms.txt` disappears, the site goes down or the
score drops**, which GitHub emails to you. No server, no account, no dependencies. Example: [issue #1](https://github.com/genzagents/citable/issues/1) watches this project's own page.

Copy [`docs/watch.yml`](docs/watch.yml) to `.github/workflows/citable-watch.yml`:

```yaml
on:
  schedule: [{ cron: "17 6 * * 1" }]   # Mondays
  workflow_dispatch:
permissions: { issues: write, contents: read }
jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: genzagents/citable@v0.3.0
        with:
          sites: |
            example.com
            docs.example.com
          # min-score: 75
```

Inputs: `sites` (required), `min-score` (fail below it), `live` (fetch as each bot, default true), `issue`
(track in an issue, default true), `notify` (login to @mention on a regression, default repo owner),
`fail-on-regression` (default true). Outputs: `score`, `regressed`, `changed`. Same thing from a shell where `gh` is logged in:

```bash
citable watch example.com docs.example.com --repo=you/your-repo --notify=you
```

**Hosted, £9/month**: the same watch run by us, no repo needed, up to 5 domains, with the weekly email.
Not live yet; press the button to tell us you want it, that is what decides whether it gets built:

[**I want the hosted weekly watch →**](https://github.com/genzagents/citable/issues/new?title=I+want+the+weekly+watch&body=Domain(s):%0A%0AWhat+would+you+pay+per+month%3F)

## Track

The watch tells you whether engines *can* read you. Track tells you whether they *do*: for the questions you want to
be the answer to, does ChatGPT, Claude or Perplexity link to your site, and who gets linked instead?

```bash
citable track example.com "best tools for X" "how do I Y" --brand=Example
```

```
## citable track: example.com

**Visibility 50%**: share of prompt-and-engine pairs where an answer linked to your site.

| Prompt | anthropic | openai | perplexity |
|---|---|---|---|
| best tools for X | **cited** | mentioned | **cited** |
| how do I Y | no | no | no |

**Who gets cited instead** (answers citing them): rival.io (4), other.org (2)
```

Engines: `anthropic` (ANTHROPIC_API_KEY, the web_search tool), `openai` (OPENAI_API_KEY, Responses API web search),
`perplexity` (PERPLEXITY_API_KEY, sonar) and `claude-code` (the `claude` CLI on your machine, no key). Every engine
whose key is present runs; `--engines=` picks a subset. Add `--issue --repo=you/repo` (or run it from the Action with
a `prompts:` input) and it keeps a "citable track: example.com" issue up to date and comments when a citation is
lost, which is the weekly email the paid tools charge $29 to $39 a month for. Bring your own keys; a 10-prompt run on
three engines costs a few cents.

## Notes

- MCP registry name: `mcp-name: io.github.genzagents/citable` (PyPI package `citable-cli`; the import and the command stay `citable`).

- Bot user agents and roles follow the vendors' published strings (OpenAI, Anthropic, Perplexity). Role "answer" = fetches
  to answer or cite in a live conversation; "train" = builds training corpora.
- The edge check compares HTTP status only. A site that returns 200 with a challenge page to bots will pass; file an issue with the domain and it gets a heuristic.
- `--no-live` skips the bot fetches (faster, robots.txt only).

## Development

```bash
python3 -m unittest discover -s tests -v
```

MIT. Built by [genzagents](https://github.com/genzagents).
