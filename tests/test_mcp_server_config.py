"""The stdio servers that need MCP SDK 1.x pinned into their own environment.

Two third-party servers declare `mcp>=1.6` with no upper bound. uv honours that
literally and installs 2.0, where the API each of them uses is gone — and both
had been failing on every boot for months, costing 11 tools while leaving a
traceback in the journal that trained everyone to skip startup errors.

What is pinned here is the shape of the fix, because the failure it prevents is
invisible from inside this process: the servers run as subprocesses, the config
looks perfectly reasonable either way, and only the journal of a running host
says which one works.
"""

from kronos.tools.mcp_servers import SDK_1X, build_mcp_config

# Servers known to be written against SDK 1.x. Verified by loading their tools
# through the app's own client on a live host: fetch 0 → 1, yahoo-finance 0 → 10.
NEEDS_SDK_1X = ("fetch", "yahoo-finance")


def test_the_servers_written_against_sdk_1x_carry_the_pin():
    """Without it they import an API that 2.0 removed and never start."""
    config = build_mcp_config()

    for name in NEEDS_SDK_1X:
        assert name in config, f"{name} is no longer configured — remove it from NEEDS_SDK_1X too"
        args = config[name]["args"]
        assert "mcp<2" in args, f"{name} lost its SDK pin and will fail on every boot"


def test_the_pin_precedes_the_command_uvx_is_asked_to_run():
    """Order is load-bearing, and getting it wrong fails as a runtime argument error.

    `uvx --with mcp<2 mcp-server-fetch` installs a dependency; `uvx
    mcp-server-fetch --with mcp<2` passes two words to the server instead.
    """
    config = build_mcp_config()

    for name in NEEDS_SDK_1X:
        args = config[name]["args"]
        entry_point = name if name == "fetch" else "mcp-yahoo-finance"
        command_index = max(i for i, arg in enumerate(args) if entry_point in arg)
        assert args.index("--with") < command_index, f"{name}: --with must come before what uvx runs"


def test_the_pin_is_two_arguments_not_one():
    """`--with mcp<2` as a single string would reach uv as an unknown flag."""
    assert SDK_1X == ["--with", "mcp<2"]


def test_nothing_else_is_pinned_backwards():
    """A blanket pin would hold healthy servers on a superseded SDK.

    Only the two packages that have stopped shipping need this; everything else
    should track the current SDK and be allowed to break loudly if it cannot.
    """
    config = build_mcp_config()

    pinned = {name for name, entry in config.items() if "mcp<2" in (entry.get("args") or [])}

    assert pinned == set(NEEDS_SDK_1X)
