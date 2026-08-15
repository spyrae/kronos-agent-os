# Acquisition — getting a page

Reading a web page is three different problems wearing one name. Most of the web
answers an ordinary GET. Some of it answers only a browser that looks human. A
little of it answers only a browser that *is* one, with a session and a history.
`fetch_page` tries them in that order and reports which one worked.

Escalation happens on evidence, never by default. Starting at the top would make
every fetch slow and burn a browser on pages that never needed one; starting at
the bottom and staying there is how a marketplace becomes "that site has no
products".

| Tier | What it is | Needs |
|---|---|---|
| `plain` | An ordinary GET | nothing |
| `stealth` | A fingerprint-spoofing browser, one page at a time | `STEALTH_FETCH_COMMAND` + a backend |
| `browser` | The stateful browser the agent already drives | `pip install -e ".[browser]"` |

The tier that worked is reported back to the model, because "this came from a
plain GET" and "this needed a fingerprint-spoofing browser" are different facts
about a source.

## What "blocked" actually means

Three sites refusing you are usually three different problems, and treating them
as one is how money gets spent on the wrong fix. Measured against the real sites
these tools target:

| | Symptom | Cause | Fix |
|---|---|---|---|
| A | Connection never establishes, or a challenge page | IP or fingerprint | the stealth tier |
| B | A page that says *"login required"* | no session | a [site account](SITE-ACCOUNTS.md) |
| C | 130 KB of markup, 300 characters of text | client-side rendering | the browser tier — sometimes nothing |

Case B is the one worth naming twice: a login wall answered with a proxy is
money spent on a problem you do not have. Read what the page says before
deciding what it needs.

## Judging a response

A tier that trusts itself is the failure mode this subsystem keeps rediscovering.
Every tier validates its own output before claiming success, because each has a
way of returning something that is not a page while looking like it did:

- **A plain fetch** can return 200 and a shell. Judged on *readable text*, not
  raw HTML — Tokopedia sends 105 KB of markup wrapped around 234 characters, and
  the ratio is the only thing that separates it from a genuinely short page.
- **A stealth backend** can exit 0 having printed its own advice. That happened:
  a misconfigured wrapper printed *"Install scrapling for CSS extraction"* and
  36 characters of advice is indistinguishable from a short page by any rule
  about pages. Backends are now checked for having returned a document at all.
- **The browser** can hand back its own error sentence. That also happened, when
  Playwright removed the API the code read pages through — the error string was
  returned as page content, so a signed-in check answered "yes" forever.

Scanning raw HTML for block markers is worse than useless, by the way: all six
measured responses contain the word "captcha" as a string constant inside a
JavaScript bundle. Marker checks run on text.

## Installing the stealth tier

Optional, and off by default. It is a ~150 MB browser binary that most
deployments never need.

```bash
bash scripts/setup-stealth.sh          # or: setup-stealth.sh /custom/dir
```

It creates a virtualenv, installs the backend, downloads the browser, proves it
can fetch a page, and prints the `STEALTH_FETCH_COMMAND` line to paste into
`.env`. Restart the agents afterwards.

**It installs outside `app/` on purpose.** `deploy.sh` rsyncs `app/` with
`--delete`, so a venv underneath it would work exactly once. The split that
survives deploys:

- the **adapter** (`scripts/stealth_fetch.py`) lives in the repo and ships with
  every deploy, so it stays in step with `acquire.py`;
- the **backend** (the venv and the browser binary) lives beside `app/`, is
  installed once per host, and is never version-controlled.

That is also why the backend is not a dependency of this package: a missing
backend is a *reported skipped tier*, not an import error at startup.

`stealth_fetch.py` writes the page to stdout and nothing else. Any command that
does the same works — the contract is a shell template containing `{url}` that
prints a document and exits 0.

## Checking that it still works

Each tier degrades quietly. The backend lives outside the repo, so a host rebuild
removes it without touching a line of code. Playwright can move an API. A plain
fetch can start meeting a CDN. None of these raise at startup, none fail a test,
and each turns into "the agent can't read that site any more" weeks later,
blamed on the site.

```bash
kaos acquire check          # probe every tier now
kaos acquire check --json   # same, machine-readable
```

`[--]` means a tier is not installed here — a choice, not a fault. `[FAIL]`
means one that should work does not.

The same probe runs daily as the `acquire-smoke` cron job, **and only speaks
when something changes**. A daily "all three tiers fine" is a message that
trains its reader to skip it, and the one morning it says something else it gets
skipped too.

It probes `example.com`, deliberately not a marketplace. The question is whether
our machinery works; whether Shopee is in a good mood today is a different
question, changes for reasons outside this repo, and a checker that goes red
whenever a marketplace tightens its defences is one people learn to ignore.

## Limits worth knowing

- **The stealth tier is not a proxy.** It changes what the browser looks like,
  not where the request comes from. A site blocking your datacentre's IP range
  is unaffected by it.
- **A residential proxy is for acting as yourself, not for reading.** Signing in
  to your own account from a datacentre is the thing that makes a site suspicious
  of *the account*. That is a different purchase from "read a public price".
- **Some pages do not yield to any tier.** Search results rendered entirely by
  client-side script, behind a session, are reported as unreadable — which is
  the honest answer, and better than an empty list presented as "no results".
- **Everything fetched is untrusted.** A product page telling the agent to
  message someone is the textbook injection, and this is the tool that carries
  it. Fetched content reaches the model framed as data, never as instructions.
