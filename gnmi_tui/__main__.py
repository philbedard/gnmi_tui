from __future__ import annotations

import argparse
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .app import TelemetryTUI
from .config import AppConfig, load_config


def comma_paths(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_duration_to_ns(value: Any) -> int:
    # Backward-compatible path: existing configs may still pass raw nanoseconds.
    if isinstance(value, int):
        if value <= 0:
            raise SystemExit("sample_interval must be greater than zero")
        return value

    text = str(value).strip().lower()
    if not text:
        raise SystemExit("sample_interval cannot be empty")

    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(ns|us|ms|s|m|h)", text)
    if not match:
        raise SystemExit(
            "Invalid sample_interval format. Use duration like 30s, 500ms, 1m, or 2h."
        )

    amount_text, unit = match.groups()
    try:
        amount = Decimal(amount_text)
    except InvalidOperation as exc:
        raise SystemExit(f"Invalid sample_interval value: {value}") from exc

    multipliers: dict[str, int] = {
        "ns": 1,
        "us": 1_000,
        "ms": 1_000_000,
        "s": 1_000_000_000,
        "m": 60 * 1_000_000_000,
        "h": 3_600 * 1_000_000_000,
    }
    interval_ns = int(amount * multipliers[unit])
    if interval_ns <= 0:
        raise SystemExit("sample_interval must be greater than zero")
    return interval_ns


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="gNMI streaming telemetry viewer (auto-updating TUI)",
    )
    parser.add_argument("--config", help="Path to YAML config file")
    parser.add_argument("--target", help="Target as host:port")
    parser.add_argument("--username", help="Target username")
    parser.add_argument("--password", help="Target password")
    parser.add_argument(
        "--path",
        dest="path_list",
        action="append",
        default=None,
        help="Subscription path. Repeat for multiple paths.",
    )
    parser.add_argument(
        "--paths",
        dest="paths_csv",
        type=comma_paths,
        default=None,
        help="Comma-separated subscription paths",
    )
    parser.add_argument(
        "--subscription-mode",
        choices=["sample", "on_change", "target_defined"],
        help="Subscription mode",
    )
    parser.add_argument(
        "--sample-interval",
        type=str,
        help="Sample interval duration, e.g. 30s, 500ms, 1m (sample mode only)",
    )
    parser.add_argument("--encoding", help="gNMI encoding (default: json_ietf)")
    parser.add_argument("--history-limit", type=int, help="Maximum unique rows in table")
    parser.add_argument("--insecure", action="store_true", default=None, help="Use insecure channel")
    parser.add_argument("--skip-verify", action="store_true", default=None, help="Skip TLS cert verification")
    parser.add_argument("--debug", action="store_true", default=None, help="Enable verbose debug logging")
    parser.add_argument("--log-file", help="Debug log file path (default: ./gnmi_tui_debug.log)")

    return parser.parse_args()


def pick(cli_value: Any, cfg: dict[str, Any], key: str, default: Any = None) -> Any:
    if cli_value is not None:
        return cli_value
    if key in cfg:
        return cfg[key]
    return default


def build_config(args: argparse.Namespace, cfg: dict[str, Any]) -> AppConfig:
    paths: list[str] = []
    if isinstance(cfg.get("paths"), list):
        paths.extend(str(p) for p in cfg["paths"])
    if args.paths_csv:
        paths.extend(args.paths_csv)
    if args.path_list:
        paths.extend(args.path_list)

    # Preserve order while removing duplicates.
    deduped_paths = list(dict.fromkeys(paths))

    if not deduped_paths:
        raise SystemExit("At least one telemetry path is required. Use --path or --paths.")

    target = pick(args.target, cfg, "target")
    username = pick(args.username, cfg, "username")
    password = pick(args.password, cfg, "password")

    if not target or not username or not password:
        raise SystemExit("Missing required settings: target, username, password")

    sample_interval_raw = pick(args.sample_interval, cfg, "sample_interval", "30s")

    return AppConfig(
        target=target,
        username=username,
        password=password,
        paths=deduped_paths,
        insecure=bool(pick(args.insecure, cfg, "insecure", True)),
        skip_verify=bool(pick(args.skip_verify, cfg, "skip_verify", True)),
        encoding=str(pick(args.encoding, cfg, "encoding", "json_ietf")),
        subscription_mode=str(pick(args.subscription_mode, cfg, "subscription_mode", "sample")),
        sample_interval=parse_duration_to_ns(sample_interval_raw),
        history_limit=int(pick(args.history_limit, cfg, "history_limit", 300)),
        debug=bool(pick(args.debug, cfg, "debug", False)),
        log_file=str(pick(args.log_file, cfg, "log_file", "gnmi_tui_debug.log")),
    )


def setup_logging(config: AppConfig) -> None:
    if not config.debug:
        return

    log_path = Path(config.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )
    logging.captureWarnings(True)

    logging.getLogger(__name__).debug(
        "Debug logging enabled",
        extra=None,
    )
    logging.getLogger(__name__).debug(
        "Runtime config: target=%s username=%s paths=%s insecure=%s skip_verify=%s encoding=%s subscription_mode=%s sample_interval=%s history_limit=%s log_file=%s",
        config.target,
        config.username,
        config.paths,
        config.insecure,
        config.skip_verify,
        config.encoding,
        config.subscription_mode,
        config.sample_interval,
        config.history_limit,
        str(log_path),
    )


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    config = build_config(args, cfg)
    setup_logging(config)
    app = TelemetryTUI(config)
    app.run()


if __name__ == "__main__":
    main()
