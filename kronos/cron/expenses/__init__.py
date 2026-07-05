"""Email-expenses pipeline (LLM-driven).

Daily job that scans Gmail for spend confirmations (Permata / Wondr / Grab and
generic receipts), extracts each expense with an LLM, audits it with a second
independent LLM pass, writes confirmed expenses to Notion via the canonical
``add_expense`` tool (FIFO IDR→RUB→USD), archives the source email, and holds
low-confidence expenses in a pending queue to be resolved from chat.

Reliability primitives that do NOT depend on per-source templates:
  * ledger  — idempotency by Gmail ``message_id`` + cross-source dedup + pending queue
  * gmail   — search / fetch / archive adapter over the Google Workspace MCP
  * processor — deterministic controller that sequences the whole run

See ``ledger.py`` for the persistence contract.
"""
