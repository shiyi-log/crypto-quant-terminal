#!/usr/bin/env python3
"""Explicitly install only the independent Demo ledger and credential tables."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_config import load_environment  # noqa: E402
from data_store import DataStore  # noqa: E402
from demo_store import DemoStore  # noqa: E402
from binance_demo_credentials import DemoCredentialStore  # noqa: E402


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    load_environment(ROOT)
    store = DataStore(root=ROOT)
    try:
        DemoStore(store).ensure_schema()
        DemoCredentialStore(store).ensure_schema()
        print(json.dumps({
            "environment": "binance_demo",
            "endpoint": "demo-fapi.binance.com",
            "schema_installed": True,
            "credentials_created": False,
            "exchange_requests": 0,
            "trading_started": False,
        }))
    except Exception as exc:
        # Database errors can include DSNs; never print raw exceptions.
        print(json.dumps({"schema_installed": False, "errorKind": type(exc).__name__}),
              file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
