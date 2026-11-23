"""Runs the black-box smoke sequence against a deployment named by env vars.

Set GRAPHRAG_SMOKE_URL and GRAPHRAG_SMOKE_TOKEN. Without them the test is reported as SKIPPED with an
explicit reason (it is a deployment check, not a unit test), never as passed.
"""

import os

import pytest
from eval.smoke import run

pytestmark = pytest.mark.skipif(
    not (os.environ.get("GRAPHRAG_SMOKE_URL") and os.environ.get("GRAPHRAG_SMOKE_TOKEN")),
    reason="deployment smoke test: needs GRAPHRAG_SMOKE_URL and GRAPHRAG_SMOKE_TOKEN",
)


async def test_deployment_smoke():
    steps = await run(os.environ["GRAPHRAG_SMOKE_URL"], os.environ["GRAPHRAG_SMOKE_TOKEN"])
    assert all(s["pass"] for s in steps), steps
