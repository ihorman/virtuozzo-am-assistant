"""Run the mock Jelastic API standalone: ``python -m mock [--port 8090]``.

Useful for extension development and offline demos (docs/PLAN.md §5).
Default account: login ``demo``, password ``demo``.
"""

from __future__ import annotations

import argparse

import uvicorn

from .server import create_app
from .state import MockConfig, MockJelastic


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Jelastic REST API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--transition", type=float, default=0.5,
                        help="seconds for async status transitions")
    args = parser.parse_args()
    state = MockJelastic(MockConfig(transition_seconds=args.transition))
    uvicorn.run(create_app(state), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
