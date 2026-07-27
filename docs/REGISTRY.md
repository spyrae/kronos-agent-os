# Skill Registry

A skill is a markdown procedure the agent will follow. Installing one runs no
code, but it does accept instructions — so the question "where did this come from,
has it changed, and does it work" has to be answerable offline.

This is a file-based registry. Sources are places you list in `registry.yaml`;
nobody runs a service, and no source becomes trusted by being reachable.

## Using it

```bash
cp registry.example.yaml registry.yaml
kaos skills search memo          # search configured sources (cached locally)
kaos skills info decision-memo   # what a source claims about a skill
kaos skills install decision-memo
kaos skills verify               # re-check everything installed
kaos skills stats                # which skills are actually used
```

`search` serves a cached index for six hours; `--refresh` refetches. A source that
is down is reported per source — one dead host does not make the others useless.

## What install decides

| Outcome | When |
|---|---|
| **active** | the source's `trust` is `signed`, the signature verifies against a key in `registry.trusted_keys`, the checksum matches, the version fits, and the skill's own scenario did not fail |
| **draft** | anything else — with the reason attached |
| **refused** | the name is taken by an installed skill or an existing tool, or the fetch failed |

A draft is not a rejection: it is the pre-existing behaviour for external skills,
and it is what you read before running `kaos skills approve <name>`. A failure never
deletes anything; the worst case is a draft you can read.

The one path that activates without a human reading the file is a signature from a
key **you** configured, over the exact bytes, on a skill whose own check passed.
That is stronger evidence than skimming markdown — which is the only reason it is
allowed to substitute for it.

## Publishing a skill

### 1. Frontmatter

```yaml
---
name: decision-memo
description: Write a one-page decision memo
version: 1.2.0
requires_kaos: ">=0.2,<0.4"
author: you@example.com
tools: [read_file]
checksum: "sha256:…"     # see below
signature: "…"           # optional, see below
---
```

Everything except `name` and `description` is optional; a skill written before this
existed installs exactly as it always did, and reports as `unverified`.

### 2. Checksum

```bash
python -c "from kronos.skills.integrity import compute_checksum; \
           print(compute_checksum('path/to/decision-memo'))"
```

Paste the result into `checksum:`. The hash covers an **allowlist** of semantic
fields — `name`, `description`, `version`, `requires_kaos`, `author`, `tools`,
`tier` — plus the body and every file under `references/`.

Two consequences worth knowing:

* Local bookkeeping is *not* hashed (`status`, `imported_from`, `imported_at`,
  `tags`, `eval_status`, and the `checksum`/`signature` fields themselves). This is
  what makes the checksum survive import, which rewrites frontmatter, and what lets
  KAOS record a local eval verdict without invalidating your checksum.
* A silently added `tools:` entry *is* hashed. Requirements a skill imposes are part
  of what you signed.

### 3. Signature (optional, required by `trust: signed` sources)

Sign the checksum string with an SSH key — the same mechanism as signed git
commits, so there is no bespoke tooling and KAOS needs no crypto dependency:

```bash
printf '%s' "sha256:…" > checksum.txt
ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n kaos-skill checksum.txt
```

Paste the body of `checksum.txt.sig` into `signature:` (the full PEM block also
works). The installing side must have your **public** key in `policy.yaml`:

```yaml
registry:
  trusted_keys:
    - "ssh-ed25519 AAAAC3... you@example.com"
```

A signature over a checksum that no longer matches is refused, not reported as
valid: the signature vouches for the checksum, so a broken checksum voids it.

### 4. A check the installer can run (recommended)

Ship a scenario next to `SKILL.md`:

```
decision-memo/
├── SKILL.md
├── references/…
└── evals/scenario.yaml
```

The format is the Phase 8 scenario — self-contained, with the model's turns
scripted inside it, so the installer replays it with no provider key and no
network. See [Evals](EVALS.md) for the fields.

By convention the scenario is a sibling of `SKILL.md`; an index entry may point
elsewhere with `scenario_url`. Note the difference in how a bad response is
treated: garbage from the *conventional* path means "no scenario here" (many hosts
answer 200 with an HTML page for a missing file), while garbage from a URL you
declared is kept and reported as your broken check.

**What that check proves and does not.** It replays your own recorded turn against
your own expectations. It catches a skill that contradicts itself — a forbidden
tool called, a budget blown, a claim missing — and a skill broken in transit. It
cannot tell anyone the skill is a good idea. So a passing scenario permits
activation and never demands it, and a skill without one installs as `unverified`
rather than being refused: most skills have none, and refusing them would leave the
registry empty and the marking meaningless.

### 5. The index

Each source publishes `index.json` at its root (`github:user/repo` reads the
repository root; `github:user/repo/subdir` reads that subdirectory):

```json
{
  "version": 1,
  "skills": [
    {
      "name": "decision-memo",
      "description": "Write a one-page decision memo",
      "version": "1.2.0",
      "url": "github:you/skills/decision-memo",
      "author": "you@example.com",
      "requires_kaos": ">=0.2",
      "checksum": "sha256:…",
      "signed": true
    }
  ]
}
```

An entry advertising a different checksum than the skill declares keeps the skill a
draft — two sources of truth disagreeing is worth stopping on.

## Trust levels

| `trust` | Required to activate | Use for |
|---|---|---|
| `signed` | signature from a configured key | an official source |
| `checksum` | nothing activates automatically; a matching checksum is still verified and reported | community sources |
| `none` | nothing activates automatically, nothing is required | a local or experimental source |

`registry.trust_default` in `policy.yaml` applies to a source that declares none.

## Usage stats and telemetry

```bash
kaos skills stats            # local: version, proof, check verdict, real loads
kaos skills stats --share    # anonymous aggregate, only with telemetry: share
```

`calls` counts real loads of a skill, not catalog listings. **Outcomes are not
tracked**: nothing in the runtime links a turn's result back to the skills that turn
loaded, so an ok-rate here would be invented. The signal for "does this skill work"
is its scenario verdict, shown alongside.

Telemetry is `off` by default and stays off unless an operator writes it in the
policy:

```yaml
registry:
  telemetry: off     # off | local | share
```

* `off` — no counters are read for sharing purposes at all.
* `local` — counters are yours; still nothing to share.
* `share` — `--share` assembles `{skill, version, calls_bucket, eval_status,
  verified}`. No content, no exact counts, no timestamps, no agent or user
  identity. Volume is bucketed (`unused`, `1-9`, `10-99`, `100+`) because an exact
  count is a workflow fingerprint.

Nothing is sent automatically in any mode — `--share` prints the payload, and where
it goes is your decision. A test asserts that no aggregate is assembled while the
mode is anything other than `share`.

## Self-improvement, measured

The same "prove it" rule applies to the agent's own persona proposals. A weekly
proposal is now measured before the owner is asked (see
[Runtime](RUNTIME.md) and `kronos/evolution_eval.py`): the patch is applied to a
copy of the workspace, the prompt is reassembled, and the offline suite runs against
both.

What it can prove is bounded, and the report says so — `measured_quality: false`.
Scripted scenarios cannot score an answer's quality, because the model's turns come
from the scenario file. What they do catch is a persona that breaks prompt assembly,
one that makes a scenario start failing, and one that repeats guidance already in
the file. Those are auto-rejected with a reason and no notification; everything else
reaches the owner with the delta attached.

```bash
/persona list              # pending proposals with their verdicts
/persona show <id>         # the full measurement
/persona list --rejected   # what the measurement refused, and why
```

Thresholds live in the `evolution` policy section (`max_regression_pct`,
`auto_reject`). A missing suite means unmeasured, never auto-rejected.

## Rollback

Delete `registry.yaml` and `kaos skills install` answers "no sources configured".
Nothing in this document changes the pre-existing `import_skill` path, and no skill
already installed is affected.
