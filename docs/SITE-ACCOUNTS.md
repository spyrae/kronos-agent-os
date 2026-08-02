# Site Accounts

Searching a site properly usually means being signed in — prices, availability
and messages all differ for a logged-in user. A site account tells the agent
which sites it may work on as you, where that session lives, and what it is
allowed to do while it is there.

Configure them in the dashboard under **Site Accounts**, or through
`/api/accounts`.

## What is stored

| | |
|---|---|
| **Site** | The handle the agent uses: `airbnb`, `booking`, `tokopedia`. |
| **Domains** | The only hosts this session may be used on — and the only ones a password is ever typed into. |
| **Login** | Your username or e-mail on that site. |
| **Login page** | Optional. Where the sign-in form lives, when it is not on the page the agent landed on. Must be one of the account's own domains. |
| **Browser profile** | A Chromium profile directory holding the session. Left blank for an account with a password, one is created per site. |
| **Password** | Optional, encrypted (see [The vault](#the-vault)). Only needed so the agent can sign in again by itself. |
| **Permission** | `read`, `message` or `full`. |
| **Approval** | Whether anything that leaves a trace waits for you. On by default. |

## Permissions

Levels are cumulative, and default to the lowest:

| Level | Covers |
|---|---|
| `read` | `read`, `search`, `login` |
| `message` | the above, plus `message`, `reply` |
| `full` | the above, plus `book`, `order`, `pay`, `review` |

An action nobody has classified is **refused**, not assumed harmless — the list
of things one can do while signed in only ever grows, and the safe reading of
"we have not classified this yet" is no.

Signing in sits at `read` on purpose: a read-only account with a stored password
exists precisely so it can keep its own session alive without asking.

With approval on (the default), anything above `read` pauses for you even when
the permission allows it. That is the part a retry cannot undo.

## Getting signed in

Two ways, and they compose:

**By hand, once.** Open the account's browser profile yourself, sign in, pass
the second factor. The agent reuses that session and stores nothing secret;
revoking is deleting the directory. When the session expires, the agent tells
you instead of guessing.

**With a stored password.** The agent fills the site's own login form when the
session expires, and carries on. The password goes from the vault into the page
and nowhere else — not into a tool result, a message, the session history, or a
log line.

Before a password is typed, the host is checked twice: once before the vault is
even opened, and again against the live URL immediately after the login step,
because submitting a login is exactly where a page can redirect. A flow that
wanders off another host gets nothing. Only the main frame is touched, so a
third-party iframe cannot present itself as the login form.

Form detection is best-effort by design. When it does not match, the answer is
"ask the owner to sign in by hand" — never a guess at which field looked like a
password box. Same when a site rejects the attempt: it is reported as a failure,
because claiming success would send the agent off to scrape a login wall and
report prices that do not exist.

## The vault

Passwords are encrypted with AES-GCM by `kronos.vault`, bound to the site they
belong to — a row copied elsewhere in the database fails to open rather than
quietly unlocking under another account's permissions.

```bash
kaos vault init      # create the key (0600), or say that VAULT_KEY already holds one
kaos vault status    # where the key lives, and which accounts depend on it
```

The key comes from `VAULT_KEY` when set, otherwise from `VAULT_KEY_PATH`
(default `./data/<agent>/vault.key`). **Keeping it in the environment is
stronger**: a key file sitting next to the database does not survive the
database being copied, which is the case encryption is here for — a backup, an
exported bundle, an rsync of one file.

There is no plaintext fallback. With no key, storing a password fails and says
so; a vault that can degrade is a plaintext store with extra steps.

Back the key up. Without it every stored password is unreadable, and
`kaos vault init --replace` will not pretend otherwise.

## What the agent can see

The model asks to work as `airbnb` and is told "ready" or "needs login". It
never receives the password, the profile path, or a way to ask for either. That
is not decoration: the same agent reads untrusted web pages for a living, and a
listing that says *"print your credentials"* or *"log in and message this
person"* has to hit a layer with nothing to give.

Tools available to it:

| Tool | Does |
|---|---|
| `list_site_accounts` | Which sites are configured and what each permits. |
| `open_site_session` | Open the owner's session, sign in again if needed, report whether it is ready. |
| `check_site_action` | Whether an action is allowed here, and whether it needs approval. |

## The trail

Every write, use and removal of a stored password is appended to
`credentials.jsonl` in the audit directory — site, purpose, whether it worked,
never the value. It is hash-chained like the other audit logs, so a removed line
shows up:

```bash
kaos audit verify
```

## Limits worth knowing

- **A password does not defeat a second factor.** A site that challenges every
  new sign-in still needs you. The profile is what makes that a rare event
  rather than a constant one.
- **Session detection is crude and pessimistic.** When unsure whether a session
  is alive, the agent says expired. The cost is one message; the alternative is
  confidently wrong data.
- **The vault protects a database that travels, not a host that is owned.** If
  someone has both your key and your data, encryption is not what stands between
  them and your accounts.
