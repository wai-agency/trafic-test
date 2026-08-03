from __future__ import annotations

import argparse
import sys

from traffic_monitor import __version__
from traffic_monitor.dashboard import serve_dashboard, write_dashboard
from traffic_monitor.monitor import run_once, watch
from traffic_monitor.recommend import print_travel_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traffic-monitor",
        description="Stau-Monitor & Reisehilfe Stuttgart → Bužim",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Einmal alle Quellen abfragen")
    check.add_argument("--config", default=None, help="Pfad zu watchpoints.yaml")
    check.add_argument(
        "--min-severity",
        choices=("info", "warning", "critical"),
        default="warning",
        help="Ab welcher Severity benachrichtigen (default: warning)",
    )
    check.add_argument("--notify", action="store_true", help="Telegram/Console-Alerts senden")
    check.add_argument("--console-only", action="store_true", help="Nur Console, kein Telegram")
    check.add_argument("--state", default=".traffic_alert_state.json", help="Dedup-State-Datei")

    watch_p = sub.add_parser("watch", help="Dauerhaft pollen und Alerten")
    watch_p.add_argument("--config", default=None)
    watch_p.add_argument("--interval", type=int, default=None, help="Sekunden zwischen Checks")
    watch_p.add_argument(
        "--min-severity",
        choices=("info", "warning", "critical"),
        default="warning",
    )
    watch_p.add_argument("--console-only", action="store_true")
    watch_p.add_argument("--state", default=".traffic_alert_state.json")

    sub.add_parser("recommend", help="Beste Abfahrt + Route + Checkliste anzeigen")

    dash = sub.add_parser("dashboard", help="Mobiles HTML-Dashboard erzeugen")
    dash.add_argument("--out", default="site", help="Ausgabeordner (default: site)")
    dash.add_argument("--config", default=None)
    dash.add_argument("--serve", action="store_true", help="Lokalen Server starten")
    dash.add_argument("--host", default="0.0.0.0")
    dash.add_argument("--port", type=int, default=8080)

    parser.epilog = """Examples:
  traffic-monitor recommend
  traffic-monitor check --notify
  traffic-monitor dashboard --out site
  traffic-monitor dashboard --serve
  traffic-monitor watch --interval 300
"""
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "recommend":
        print_travel_plan()
        return 0

    if args.command == "check":
        run_once(
            args.config,
            min_severity=args.min_severity,
            notify=args.notify or args.console_only,
            console_only=args.console_only,
            state_path=args.state,
        )
        return 0

    if args.command == "watch":
        watch(
            args.config,
            interval=args.interval,
            min_severity=args.min_severity,
            console_only=args.console_only,
            state_path=args.state,
        )
        return 0

    if args.command == "dashboard":
        write_dashboard(args.out, args.config)
        if args.serve:
            serve_dashboard(args.out, host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
