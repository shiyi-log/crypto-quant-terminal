#!/usr/bin/env python3
"""Compatibility entry point for the former DSH emergency sender.

The original receipt-order diagnosis is preserved in
DSH_BUGREPORT-direct_codex-method-check.md. Native delivery is now fixed in
direct_codex.py, so this command uses the same validation, inactive-turn
fallback and delivery journal as desktop_link.py. It has no separate RPC or
automatic retry path.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

import desktop_link
import direct_codex


FixedSendError = desktop_link.LinkError


def load_thread_id():
    return desktop_link.load_config()["codex_thread_id"]


def deliver(text, thread_id=None, timeout=20):
    """Legacy Python adapter; preserve the main native delivery safeguards."""
    return direct_codex.deliver_codex(thread_id or load_thread_id(), text, timeout=timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text")
    parser.add_argument("--id", dest="message_id")
    parser.add_argument("--reply-to")
    parser.add_argument("--thread", help="仅本次发送覆盖绑定目标，不保存绑定")
    parser.add_argument("--probe", action="store_true", help="明确发送一次最小探测")
    args = parser.parse_args(argv)
    text = (f"DSH_PROBE {time.strftime('%Y-%m-%dT%H:%M:%S%z')}" if args.probe
            else args.text if args.text is not None else sys.stdin.read())
    try:
        config = desktop_link.load_config()
        if args.thread:
            config = {**config, "codex_thread_id": args.thread}
        receipt = desktop_link.send(
            config, "codex", text, message_id=args.message_id, reply_to=args.reply_to,
        )
        # Preserve the old command's top-level native receipt fields. A duplicate
        # has no new turn receipt because nothing is sent for it.
        native = receipt.get("result", {})
        print(json.dumps({**native, **receipt}, ensure_ascii=False))
        return 0
    except (desktop_link.LinkError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"id": args.message_id, "ok": False, "error": str(exc)},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
