"""Run the ABGES MCP control-plane worker continuously or once."""
from __future__ import annotations

import argparse
import json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abges_mcp.control_plane import run_worker_forever, run_one_worker


def main(argv=None):
    parser = argparse.ArgumentParser(description="ABGES MCP worker")
    parser.add_argument("--once", action="store_true", help="claim and execute at most one queued job")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.once:
        print(json.dumps(run_one_worker(), ensure_ascii=False, indent=2))
        return 0
    run_worker_forever(poll_seconds=args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
