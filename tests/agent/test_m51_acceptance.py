"""A completed route with REVISE is not an accepted M5 demo."""

import pytest

from scripts.run_m5_demo import assert_accepted


@pytest.mark.parametrize("status,accepted", [("PASS", True), ("REVISE", False),
                                             ("BLOCK", False)])
def test_m5_demo_requires_verifier_pass(status, accepted):
    record = {"name": "both", "plan": {"route": "BOTH"},
              "sql_results": [{}], "rag_results": [{}],
              "trace": ["planner", "fork", "data_analyst", "knowledge_researcher",
                        "synthesis", "verifier"], "status": status}
    if accepted:
        assert_accepted(record, "BOTH")
    else:
        with pytest.raises(RuntimeError, match="verification failed: both"):
            assert_accepted(record, "BOTH")
