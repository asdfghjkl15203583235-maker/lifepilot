"""Evaluation tests hit the real LLM and DB, so they are opt-in.

Run them with:

    RUN_EVALUATION_TESTS=1 uv run pytest tests/evaluation -q

Plain `uv run pytest` skips this directory entirely (collection is ignored).
"""

import os

if os.environ.get("RUN_EVALUATION_TESTS") != "1":
    collect_ignore_glob = ["test_*.py"]
