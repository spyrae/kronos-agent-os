# ADR-0002 — DNS-pinned egress for public-web acquisition

- **Status:** accepted
- **Date:** 2026-09-07
- **Context:** review finding F01 (SSRF)

## Problem

Initial-URL checks do not cover DNS rebinding, redirects or browser subrequests.
The deployment egress allowlist intentionally permits trusted local services;
it cannot double as a public-page SSRF guard. A failed browser navigation also
used to return HTML from the previous, possibly authenticated, page.

## Options

1. Validate hostnames before each navigation: small change, but the browser can
   resolve them again and subresources bypass the initial check.
2. Intercept browser requests in Playwright: more coverage, but redirects,
   service workers and other browser transports need separate enforcement; a
   DNS check followed by browser fetching still has a check/use race.
3. A local HTTP/CONNECT proxy shared by the acquisition backends: adds a small
   transport component, but validation and socket creation share one boundary.

## Decision

Use option 3, without new dependencies or deployment configuration changes.
Resolve the target once, reject the whole answer if any address is non-public,
then connect to the validated numeric IP. Public-web HTTP requests, browser
redirects and subresources go through the guard. TLS remains end-to-end: no
certificate installation, interception or disabled certificate verification.

Chromium launches with an explicit proxy, the `<-loopback>` bypass subtraction,
QUIC disabled and non-proxied WebRTC UDP disabled. Both persistent and temporary
profiles use the same launch contract. The maintained stealth adapter gets its
proxy explicitly; arbitrary commands are skipped because setting `HTTP_PROXY`
is not an enforceable contract for an unknown executable. Existing documented
`<python> <repo>/scripts/stealth_fetch.py {url}` configuration remains valid.
Standalone invocation of that adapter now requires `--proxy`.

Security denials are terminal, not a reason to try a less guarded backend. A
blocked subrequest poisons the proxy session. Failed navigation cannot produce
content from the previous page. Listeners and active tunnels close with their
owning fetch/browser session. Trusted integration endpoints remain unaffected.

## Boundaries and trade-offs

This is a public-fetch boundary, not an OS sandbox for arbitrary tools or a
compromised browser binary. Operator-defined MCP programs and local LLM calls
are outside this boundary. Public hostnames resolving to mixed public/private
answers, IPv6 transition addresses, and unsupported stealth adapters fail
closed. Any deployment requiring those cases needs a separately reviewed
adapter/egress design, not a silent bypass. Profile browsing after a denied
subresource requires a new browser session.

Loopback socket tests cover the actual proxy wire protocol and HTTP redirect
path. Browser launch-contract tests are mocked: live Chromium/CloakBrowser
validation remains required on a host with those optional backends installed.

## References

- [Playwright proxy configuration](https://playwright.dev/python/docs/api/class-browsertype)
- [Chromium implicit bypass rules](https://chromium.googlesource.com/chromium/src/+/show/main/net/docs/proxy.md)
