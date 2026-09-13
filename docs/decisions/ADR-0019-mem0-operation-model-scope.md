# ADR-0019: Per-operation Mem0 model and invocation scope

**Date:** 2026-09-08
**Status:** accepted

## Context

Mem0 creates its own DeepSeek SDK outside the runtime factory. Its response parser
discards usage, and some releases invoke extraction in internal worker threads.
The local environment has no mem0 package; the private production inventory lists
mem0ai 1.0.7. Package-level acceptance must therefore remain a separate gate.

The main invocation also established audit context only around the model loop.
Retrieval and compaction ran outside it, and run_in_executor did not propagate
ContextVars to background storage. Admission inside a model adapter alone would
not repair original-chat attribution or explicit lite policy in these paths.

## Requirements

- Admit every actual Mem0 completion, including multiple extraction/update calls.
- Account the original response before Mem0 parsing removes usage or fails.
- Preserve normal provider options/parser and use configured lite on downgrade.
- Carry audit, budget and execution context through Mem0's own workers without
  sharing mutable caller state on its cached instance.
- Cover retrieval, background storage and compaction with invocation context.
- Do not install dependencies, mutate global Mem0 registries or claim live acceptance.

## Decision

Wrap the cached Memory object with BudgetedMemory. Only add/search/get_all, the
operations KAOS currently uses, are exposed. Each operation shallow-copies the
Memory adapter while sharing its existing vector/history resources. It attaches
a private ScopedMemoryLlm: a shallow adapter copy with only the SDK completion
capability replaced. The original client remains owned by the original adapter.

Each generate_response enters a fresh copy of the operation's captured Context,
including from a raw ThreadPoolExecutor worker. The completion boundary admits
immediately before dispatch and records the response before returning it to the
original parser. A downgrade invokes the configured lite factory with the same
messages, tool schema/choice and generation options. Its reply is adapted to the
fields the Mem0 DeepSeek parser reads; factory callbacks account it only once.

Unsupported adapter capabilities, graph stores and rerankers fail explicitly.
New operations are not silently forwarded. A missing DeepSeek key no longer lets
Memory.from_config construct its implicit default API provider. This does not
disable FTS fallback inside search_memories, nor does it fix graph-level A05.

Move the complete invocation inside audit/model-budget context, preserving the
existing plan execution scope. Submit background storage through copy_context.run.
Existing model-loop context remains nested and is restored normally.

## Alternatives

### Register a new global Mem0 LLM provider

Rejected: registration mutates a process-global map, while upstream config
validation separately enumerates supported names. It is not a reliable local
extension point for the currently unpinned optional package range.

### Check only before mem.add

Rejected: one add can make multiple completions, lose thread context, and discard
usage. An outer preflight neither accounts nor protects each actual dispatch.

### Replace the singleton's context/LLM before each operation

Rejected: concurrent callers would race and attribute one user's calls to another
session. Per-operation copies retain shared storage without shared caller state.

## Consequences

### Positive

- Original chat attribution and lite policy cover ancillary invocation work.
- Normal Mem0 provider parsing/options remain unchanged and returned usage survives
  parser failures. Lite adaptation preserves tool calls without double charging.
- Concurrent internal workers each enter their own Context copy.

### Negative

- Shallow adapter copies rely on the inspected sync Memory/DeepSeek contract.
  Actual mem0ai 1.0.7 with local stores and fake network still needs acceptance;
  reading upstream source is not equivalent to executing the installed package.
- SDK-internal retries, unknown outcomes, best-effort recorder writes, durable
  session totals and monetary reservations remain unresolved F12 requirements.
- This does not fix unowned background-writer/reset lifecycle (F13), synchronous
  retrieval/compaction responsiveness (F15), cold-start singleton concurrency,
  or user-scoping of every memory store (A02).
- Keyless graph memory remains disabled (A05); no policy/config rollout is implied.

### Neutral

- No dependency, schema, environment setting or production service is changed.
- Future graph/reranker/API additions require an explicit model-boundary design.

## Verification

Fake Mem0/provider objects reproduce its own ThreadPoolExecutor and parser shape,
using real isolated shared/session ledgers and the runtime factory. Tests cover
per-dispatch refusal, lite JSON/tools, parser failure after usage, two concurrent
callers, simultaneous internal workers, execution stop, unsupported capabilities,
and no implicit default provider. A real BaseChatModel with cost callbacks verifies
one lite charge. A full agent invocation verifies retrieval/background/compaction
attribution, explicit lite propagation and context restoration.

## References

- [Mem0 1.0.7 Memory implementation](https://raw.githubusercontent.com/mem0ai/mem0/v1.0.7/mem0/memory/main.py).
- [Mem0 DeepSeek adapter](https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/llms/deepseek.py)
  and [config validation](https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/llms/configs.py):
  current source context, not proof of the production package's exact contents.
- `tests/test_mem0_budget.py`, ADR-0015, ADR-0017 and the F12 register.
