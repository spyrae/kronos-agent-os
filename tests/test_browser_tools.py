"""Browser tool outputs must be flagged untrusted.

Everything a browser returns comes from an attacker-controllable web page, so
the engine has to wrap it as data (not trusted text) before the model reads it.
This asserts the flag is set at import time — independent of whether playwright
is installed — so the wrapping actually kicks in.
"""

from kronos.tools.browser.tools import _BROWSER_TOOLS


def test_browser_tools_flag_output_as_untrusted():
    assert _BROWSER_TOOLS  # sanity: the list is populated
    for tool in _BROWSER_TOOLS:
        assert (tool.metadata or {}).get("untrusted_output") is True, tool.name
