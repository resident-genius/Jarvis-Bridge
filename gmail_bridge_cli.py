"""
gmail_bridge_cli.py -- headless command-line entry point for the bridge.
Usage:
    python gmail_bridge_cli.py [--config PATH] [--once | --init]
Without --once it runs the Loop A / Loop B poll loop forever (this is what
the "JarvisBridgeMail" scheduled task / "Start Jarvis Bridge" shortcut runs).
With --once it does a single poll pass and exits (handy for testing).
With --init it only does first-run setup (default config, every executor
inbox with done\\ and logs\\, the workspace allowlist) and exits.
Only one bridge (GUI or CLI) runs per config file; a second one exits.
"""
import argparse
import os
import signal
import sys
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge_core import (Bridge, SingleInstance, bridge_setting,  # noqa: E402
                         init_install)
EXIT_ALREADY_RUNNING = 3
CONFIG_NAME = bridge_setting("paths.config_file_name",
                             "gmail-bridge-config.json")
def main():
    ap = argparse.ArgumentParser(description="Jarvis bridge (Gmail bus) -- CLI")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="single poll pass, then exit")
    mode.add_argument("--init", action="store_true",
                      help="first-run setup only, then exit")
    args = ap.parse_args()
    log = lambda line: print(line, flush=True)  # noqa: E731
    if args.init:
        created = init_install(args.config, log)
        log("Init done: %d item(s) created." % len(created))
        return 0
    lock = SingleInstance(args.config)
    if not lock.acquire():
        log("Another Jarvis bridge is already running for %s; exiting."
            % args.config)
        return EXIT_ALREADY_RUNNING
    try:
        init_install(args.config, log)
        bridge = Bridge(args.config)
        if args.once:
            bridge.run_once()
            return 0
        stop = threading.Event()
        def _sig(*_a):
            stop.set()
        signal.signal(signal.SIGINT, _sig)
        try:
            signal.signal(signal.SIGTERM, _sig)
        except (AttributeError, ValueError):
            pass
        bridge.run_forever(stop)
        return 0
    finally:
        lock.release()
if __name__ == "__main__":
    sys.exit(main())
