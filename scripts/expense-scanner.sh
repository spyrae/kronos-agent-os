#!/bin/bash
# expense-scanner.sh — Scan Gmail for bank transaction emails
#
# TODO: This script depends on mcporter (google-workspace MCP tool) which does
#       not exist in Kronos Agent OS. It needs to be rewritten using one of the following:
#
#       Option A: Route through the Kronos Agent OS agent — send a structured message
#         to the bridge webhook asking the agent to check Gmail for transaction
#         emails. The agent has access to MCP tools (google-workspace, etc.) and
#         can write results to /opt/kaos/app/workspace/PENDING-EXPENSES.md.
#
#       Option B: Use the Gmail API directly with OAuth 2.0 credentials:
#         - Store client credentials in /opt/kaos/app/.env
#         - Use curl + python3 to authenticate and fetch messages
#         - Parse bank transaction email format
#
#       Option C: Use the existing google-workspace MCP via an HTTP API call
#         to the Kronos Agent OS bridge, which will proxy the request through the agent.
#
#       Until rewritten, this script exits with an error explaining the issue.
#
# Original: Kronos I scripts/expense-scanner.sh
# Blocked by: mcporter removal in Kronos Agent OS
#
# NOTE: The transaction email parsing logic (python3 inline script) is preserved
#       below as a reference for the rewrite. The parse_permata_email() function
#       handles both Indonesian and English email formats and both ID/US number
#       separators.

# --- Preserved parser (for rewrite reference) ---
#
# parse_transaction_email() {
#   local content="$1"
#   python3 << 'PYEOF' "$content"
# import sys, re, json
# content = sys.argv[1]
# patterns = {
#     'date':     [r'Tanggal\s*:\s*(.+)', r'Date\s*:\s*(.+)'],
#     'time':     [r'Jam\s*:\s*(.+)', r'Time\s*:\s*(.+)'],
#     'amount':   [r'Nominal\s*:\s*IDR\s*([\d.,]+)', r'Amount\s*:\s*IDR\s*([\d.,]+)'],
#     'ref':      [r'Nomor Referensi\s*:\s*(\S+)', r'Reference Number\s*:\s*(\S+)'],
#     'category': [r'Kategori\s*:\s*(.+)', r'Category\s*:\s*(.+)'],
#     'status':   [r'Status Transaksi\s*:\s*(.+)', r'Transaction Status\s*:\s*(.+)'],
# }
# result = {}
# for field, pats in patterns.items():
#     for pat in pats:
#         m = re.search(pat, content, re.IGNORECASE)
#         if m:
#             result[field] = m.group(1).strip()
#             break
# if 'amount' in result:
#     raw = result['amount']
#     if '.' in raw and ',' in raw:
#         if raw.rindex(',') > raw.rindex('.'):
#             raw = raw.replace('.', '').replace(',', '.')
#         else:
#             raw = raw.replace(',', '')
#     elif ',' in raw and '.' not in raw:
#         parts = raw.split(',')
#         if len(parts[-1]) == 2:
#             raw = raw.replace(',', '.')
#         else:
#             raw = raw.replace(',', '')
#     result['amount'] = raw
# if 'date' in result and 'amount' in result:
#     print(json.dumps(result))
# else:
#     print('{}')
# PYEOF
# }

echo "ERROR: expense-scanner.sh is not yet implemented for Kronos Agent OS."
echo ""
echo "This script requires rewriting to remove the mcporter dependency."
echo "See the TODO comments in this file for implementation options."
echo "The transaction email parser logic is preserved in the comments above."
echo ""
echo "Blocked: mcporter CLI is not available in Kronos Agent OS."
exit 1
