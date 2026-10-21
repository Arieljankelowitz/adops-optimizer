"""Wake the self-hosted model and wait until it is ready (up to the readiness budget).

    set -a; source .env; set +a
    uv run python scripts/wake_model.py

Starts a GPU container if none is running, which is billed until it scales down.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts.config import RunBudget  # noqa: E402
from llm.client import ModelClientError, create_client  # noqa: E402


def main() -> int:
    try:
        client = create_client(os.environ, RunBudget())
        waited = client.wait_until_ready(
            on_poll=lambda elapsed: print(f"waiting... {elapsed:.0f} s", flush=True))
    except ModelClientError as exc:
        print(f"not ready: {exc}", file=sys.stderr)
        return 1
    print(f"ready after {waited:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
