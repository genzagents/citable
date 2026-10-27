---
name: citable
description: Use when someone asks whether their website shows up in, is read by, or gets cited by ChatGPT, Claude, Perplexity or AI search (AEO, GEO, "AI visibility", llms.txt, GPTBot, ClaudeBot), or asks for an llms.txt. Runs the citable check and turns the findings into concrete edits.
---

# citable

`citable` answers one question: can AI answer engines fetch and cite this site? It checks the things
that actually stop citations (robots.txt rules for the *answer* bots, CDN/WAF blocks found by fetching
as each bot, JS-only pages with no server-rendered text) and the things that help (llms.txt, JSON-LD,
title/description, sitemap).

## How to run it

Call the `citable` MCP server that ships with this plugin: `check_site` with the domain, and `draft_llms_txt`
when the report says /llms.txt is missing or thin. If the tools are not available, ask the user to enable the
citable plugin's MCP server rather than running code from the shell.

## How to read the result

1. **FAIL on "robots.txt allows answer engines" or "not blocked at the edge" is the only thing that matters first.**
   A site blocked for OAI-SearchBot, ChatGPT-User, Claude-SearchBot, Claude-User or PerplexityBot cannot be cited,
   whatever else it does. Edge blocks (HTTP 403/429/503 to the bot while a browser gets 200) are a CDN or WAF
   setting, not robots.txt: Cloudflare "Block AI bots", bot fight mode, or a rate limiter. Say which setting to change.
2. Training bots (GPTBot, ClaudeBot, Google-Extended, CCBot) are reported as INFO. Blocking them is a legitimate
   choice and does not affect citations. Do not tell people to unblock them unless they want to be training data.
3. **Content readable without JavaScript** failing means the pages are a client-rendered shell. The fix is
   server-side rendering or prerendering for the pages that should be cited; nothing else helps until that is done.
4. llms.txt, JSON-LD, title/description, Open Graph and sitemap are incremental: apply the fixes in the order
   listed in the report, and offer to write the files (llms.txt draft, a JSON-LD Organization block, meta tags).

## When you make edits for the user

- Put `/llms.txt` at the site root as plain text (`text/plain`), starting with `# Site name`, then a `> one-line summary`,
  then markdown links to the pages worth citing. Keep it short; `/llms-full.txt` is for the long version.
- JSON-LD goes in `<script type="application/ld+json">` in `<head>`; use `Organization` or `WebSite` on the homepage,
  `Article`/`BlogPosting` with `dateModified` on posts, `FAQPage` for Q&A.
- After edits, run the check again and show the before/after score.
