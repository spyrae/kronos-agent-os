#!/usr/bin/env python3
"""Fetch one page through a stealth browser and print its HTML.

The middle tier of `kronos.tools.acquire` runs a *command*, not an import, so
the heavyweight browser stack stays out of the agent's process and out of this
package's dependencies. This is the adapter for that contract: stdout is the
page, stderr is the reason there isn't one, and the exit code says which.

It lives in the repo — and the backend it drives does not — on purpose. The
adapter is twenty lines that change with `acquire.py`; the backend is a browser
binary that changes with the host. Deploying the first and installing the second
separately is what keeps a machine-specific path out of version control while
still making the wiring reproducible. `scripts/setup-stealth.sh` installs the
backend and prints the line that connects the two.

**Nothing but the page goes to stdout.** That rule is not stylistic. The
previous wrapper here was a general-purpose scraper that, when its optional
parser was absent, printed *"Install scrapling for CSS extraction"* and exited
0 — and 37 characters of advice is indistinguishable from a short page to
anything downstream. `acquire.py` now validates what a backend returns, but the
backend should not have been lying in the first place. A script that can only
print a page cannot print advice instead of one.
"""

import argparse
import sys

# A stealth browser is slow by construction; the caller's own ceiling is 90s.
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000

# Time to let client-side rendering finish after the document is ready. Most
# pages need none of it; the ones this tier exists for need all of it.
DEFAULT_SETTLE_MS = 3_000

EXIT_BACKEND_MISSING = 2
EXIT_NO_PAGE = 3
EXIT_FETCH_FAILED = 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="the page to fetch")
    parser.add_argument(
        "--settle",
        type=int,
        default=DEFAULT_SETTLE_MS,
        metavar="MS",
        help=f"wait this long after load for client-side rendering (default: {DEFAULT_SETTLE_MS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_NAVIGATION_TIMEOUT_MS,
        metavar="MS",
        help=f"navigation timeout (default: {DEFAULT_NAVIGATION_TIMEOUT_MS})",
    )
    args = parser.parse_args(argv)

    try:
        from cloakbrowser import launch
    except ImportError as e:
        # Not a crash worth a traceback: on a host that never installed the
        # backend this is the expected answer, and the caller reports it as a
        # skipped tier.
        print(f"stealth backend not installed in this interpreter ({e}); see scripts/setup-stealth.sh", file=sys.stderr)
        return EXIT_BACKEND_MISSING

    try:
        html = _fetch(launch, args.url, timeout_ms=args.timeout, settle_ms=args.settle)
    except Exception as e:
        print(f"stealth fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FETCH_FAILED

    if not html.strip():
        print("the browser returned an empty document", file=sys.stderr)
        return EXIT_NO_PAGE

    sys.stdout.write(html)
    return 0


def _fetch(launch, url: str, *, timeout_ms: int, settle_ms: int) -> str:
    """Drive the browser and hand back the document it ended up with."""
    browser = launch(headless=True)
    try:
        page = browser.new_page()
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
        return page.content()
    finally:
        # Closing matters more than the result: a leaked headless browser on a
        # host running six agents is a slow memory leak nobody attributes here.
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
