# Repositories

"Why did the build break", "what changed this week", "show me that function" —
questions with a definite answer, usually asked when the laptop is shut. The
agent can answer them by reading a working copy on the machine it runs on.

Read-only. See [Limits](#limits) for what that excludes and why.

```bash
kaos repos add kaos ~/projects/kronos-agent-os/app --notes "the agent's own code"
kaos repos list
kaos repos remove kaos
```

Registering is the moment the boundary is drawn — nothing outside a registered
root is reachable. The repository tools appear only once at least one is
registered, so an agent with none is not handed six tools that all refuse.

## Tools

| Tool | Answers |
|---|---|
| `repo_list` | which repositories it can read |
| `repo_tree` | what files are there |
| `repo_read` | a file, or a range of lines, numbered |
| `repo_search` | which lines match a pattern |
| `repo_history` | recent commits, optionally for one path |
| `repo_diff` | uncommitted work, or the difference against a ref |

Output is bounded — a file range, forty matches, a truncated diff — and long
answers say how to ask for the rest rather than silently stopping.

## The boundary

A directory tree has no edges unless someone draws them. Three are drawn, and
each exists because of a specific way this goes wrong:

**Only registered roots.** Without this, "read a file" is "read any file on the
host".

**Never outside the root.** Every path is resolved — symlinks included — before
it is compared against the root. `docs/../../../../etc/passwd` and a symlink
called `notes` pointing at `/` are the same request written twice.

**Secrets are not source.** A repository contains `.env`, keys, tokens and a
`data/` directory nobody meant to publish. Reading those into a model's context
is a leak no later redaction undoes, so they are refused *before* being read:

- by name — `.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.p8`, `credentials.json`,
  `*.session`, `*.db` and similar, matched on every path segment so
  `config/.env.production` fails too;
- by the repository's own `.gitignore`, which is a strong hint about what is
  local state rather than code;
- and a search never returns a line from a file that would have been refused.

What does get through is passed through the same secret redaction the audit log
uses, and marked **untrusted**: a README or a code comment is as good a place to
hide an instruction aimed at an agent as a web page is.

## Limits

- **Nothing is written.** No commit, no branch, no push, no PR. Doing that well
  needs credentials and a decision about which repositories may be changed
  unattended; until that exists, a permission other than `read` is refused
  rather than quietly accepted.
- **The agent reads the machine it runs on.** A repository that lives on your
  laptop is not visible to an agent on a server. Nothing here clones anything.
- **`.gitignore` is a hint, not a guarantee.** It is honoured because what a
  project ignores is usually its secrets and its data, but the name-based
  refusals are the actual rule.
- **A working copy is not the remote.** `repo_history` and `repo_diff` show what
  that checkout knows; a branch nobody fetched is not there.
