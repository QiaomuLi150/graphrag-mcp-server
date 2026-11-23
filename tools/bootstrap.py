"""Container bootstrap for the one-command local stack: wait for Neo4j, create dev keys, seed (idempotent)."""

from __future__ import annotations

import os
import sys
import time

import neo4j

from tools import dev_token, seed_demo


def main() -> int:
    dev_token.init()
    uri, pw = os.environ["GRAPHRAG_SEED_URI"], os.environ["GRAPHRAG_SEED_PASSWORD"]
    for _ in range(60):
        try:
            with neo4j.GraphDatabase.driver(uri, auth=("neo4j", pw)) as d:
                d.verify_connectivity()
            break
        except Exception:  # noqa: BLE001
            time.sleep(2)
    else:
        print("neo4j not reachable", file=sys.stderr)
        return 1
    return seed_demo.main(["--dataset", "data/demo", "--edition", "community", "--provision-reader", "--prune"])


if __name__ == "__main__":
    raise SystemExit(main())
