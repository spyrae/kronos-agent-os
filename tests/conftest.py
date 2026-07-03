import pytest


@pytest.fixture(autouse=True)
def reset_prompt_shield_rate_limiter():
    """Keep tests isolated from the process-global prompt shield limiter."""
    from kronos.security.shield import rate_limiter

    rate_limiter._buckets.clear()
    yield
    rate_limiter._buckets.clear()
