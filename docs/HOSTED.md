# citable hosted: how the £9/month tier runs

No server. One always-on machine runs `citable hosted run` once an hour. Everything else is Polar (payments,
checkout form, receipts, VAT) and Resend (email delivery).

## What a subscriber sees

1. Pays on the Polar checkout. The checkout asks for three custom fields: their site(s), the prompts they want
   to be the answer to, and their brand name.
2. Within the hour: "Your first citable report" with the full check of every site (bots, edge blocks, llms.txt,
   schema, JS-only content, fixes) and the prompt tracking table (cited / mentioned / no per engine, who gets cited
   instead).
3. Then: an alert email the moment anything regresses (an answer bot blocked, llms.txt gone, site down, score drop,
   an engine that stopped citing them), and a weekly digest otherwise. Improvements are reported too.
4. Replies to the email reach the sender address; cancellation is on the Polar receipt. Nobody is in the loop.

## One-time setup (about 10 minutes, needs the Polar and Resend accounts)

### Polar

1. Create a product: "citable hosted watch", recurring, £9/month (or $12). Description: the three bullets above.
2. Checkout custom fields (Settings -> Custom fields, then enable them on the product). The runner reads them from
   `custom_field_data` on each subscription, keyed by slug:

   | Slug | Type | Required | Label shown at checkout |
   |---|---|---|---|
   | `site` | textarea | yes | Your site(s), one per line (up to 5) |
   | `prompts` | textarea | no | Questions you want to be the answer to, one per line (up to 10) |
   | `brand` | text | no | Your brand name (so mentions without a link count) |

   Slugs `sites`, `domain`, `domains`, `website`, `url` also work for the first field; `queries` or `questions` for the second.
3. Create an organisation access token with `subscriptions:read` (Settings -> Access tokens). Copy the product id.
4. Put the checkout link on the page: replace the "Tell me when the checkout is live" button in `docs/index.html`
   with the Polar checkout URL.

### Resend

1. Add and verify a sending domain (DNS records) or start on the shared `onboarding@resend.dev` sender for the first tests.
2. Create an API key with sending permission.

### The machine

```bash
export POLAR_ACCESS_TOKEN=polar_oat_...      # subscriptions:read
export POLAR_PRODUCT_ID=...                  # optional; otherwise every active subscription of the org
export RESEND_API_KEY=re_...
export CITABLE_FROM="citable <watch@yourdomain.com>"
export ANTHROPIC_API_KEY=...                 # any of anthropic / openai / perplexity keys; or `claude` on PATH
citable hosted run --dry-run                 # prints what it would send; sends nothing
citable hosted run                           # sends
citable hosted launchd > ~/Library/LaunchAgents/io.genzagents.citable.hosted.plist   # then add the secrets to the plist
launchctl load -w ~/Library/LaunchAgents/io.genzagents.citable.hosted.plist
```

Secrets live in the plist's `EnvironmentVariables` (or a wrapper script that reads the keychain), never in the repo.
State lives in `~/.citable-hosted/<subscription id>.json`: the last snapshot per site, when the last email went, when the
last tracking run was. Delete a file to resend a first report.

## Costs per subscriber per month

- Checks: a dozen HTTP requests per site per hour. Free.
- Tracking: 10 prompts x 3 engines once a week = 120 answers a month, a few cents with API keys, free via Claude Code.
- Polar: 4% + 40c per payment, VAT handled. Resend: free tier covers thousands of emails.

So the £9 is almost entirely margin and the only operational task is keeping the machine on.

## Running it for your own customers

`citable hosted run --subscribers=subs.json` skips Polar and reads a list of `{id, status, customer: {email, name},
custom_field_data: {site, prompts, brand}}` objects (the same shape Polar returns), so an agency can run the watch for
its clients from a file without any payment provider.
