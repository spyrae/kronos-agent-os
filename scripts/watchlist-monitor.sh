#!/bin/bash
# watchlist-monitor.sh — Check watchlist tickers for significant price moves
#
# TODO: This script depends on mcporter (yahoo-finance MCP tool) which does not
#       exist in Kronos II. It needs to be rewritten using one of the following:
#
#       Option A: Direct Yahoo Finance API calls via curl (no auth required):
#         https://query1.finance.yahoo.com/v8/finance/chart/AAPL?interval=1d&range=5d
#         Parse JSON response with python3.
#
#       Option B: Route through the Kronos II agent — send a structured message
#         to the bridge webhook asking the agent to check prices and respond with
#         results. The agent has access to MCP tools (yahoo-finance, etc.).
#
#       Option C: Use a different finance API (e.g., Alpha Vantage, Finnhub) with
#         a direct API key stored in /opt/kronos-ii/app/.env.
#
#       Until rewritten, this script exits with an error explaining the issue.
#
# Original: Kronos I scripts/watchlist-monitor.sh
# Blocked by: mcporter removal in Kronos II
# Reads watchlist from: /opt/kronos-ii/workspace/self/skills/news-monitor/references/WATCHLIST.md

echo "ERROR: watchlist-monitor.sh is not yet implemented for Kronos II."
echo ""
echo "This script requires rewriting to remove the mcporter dependency."
echo "See the TODO comments in this file for implementation options."
echo ""
echo "Blocked: mcporter CLI is not available in Kronos II."
exit 1
