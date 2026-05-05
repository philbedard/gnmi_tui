from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from pygnmi.client import gNMIclient

from .config import AppConfig


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class TelemetryEvent:
    timestamp_ns: int
    target: str
    path: str
    value: Any
    update_type: str = "update"

    @property
    def iso_time(self) -> str:
        ts = self.timestamp_ns / 1_000_000_000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


@dataclass(slots=True)
class StatusEvent:
    level: str
    message: str
    at_ns: int


StreamEvent = TelemetryEvent | StatusEvent


def join_xpath(prefix: str, leaf: str) -> str:
    if not prefix:
        return leaf or "/"
    if not leaf:
        return prefix
    if prefix.endswith("/") and leaf.startswith("/"):
        return prefix[:-1] + leaf
    if not prefix.endswith("/") and not leaf.startswith("/"):
        return f"{prefix}/{leaf}"
    return prefix + leaf


def xpath(path: Any) -> str:
    if isinstance(path, str):
        return path

    elems = getattr(path, "elem", None)
    if elems is not None:
        parts: list[str] = []
        for elem in elems:
            name = getattr(elem, "name", "")
            if not name:
                continue

            keys = getattr(elem, "key", None)
            if keys:
                key_text = "".join(f"[{k}={v}]" for k, v in sorted(keys.items()))
                parts.append(f"{name}{key_text}")
            else:
                parts.append(name)
        return "/" + "/".join(parts) if parts else ""

    element = getattr(path, "element", None)
    if element:
        return "/" + "/".join(str(part) for part in element)

    # Some protobuf Path objects have no elem/element (e.g., prefix with only target).
    # Return empty path instead of protobuf string representation.
    if any(hasattr(path, attr) for attr in ("target", "origin")):
        return ""

    if isinstance(path, dict):
        elems = path.get("elem")
        if isinstance(elems, list):
            parts: list[str] = []
            for elem in elems:
                if not isinstance(elem, dict):
                    continue
                name = elem.get("name", "")
                if not name:
                    continue
                keys = elem.get("key", {})
                if isinstance(keys, dict) and keys:
                    key_text = "".join(f"[{k}={v}]" for k, v in keys.items())
                    parts.append(f"{name}{key_text}")
                else:
                    parts.append(name)
            return "/" + "/".join(parts)

        if "path" in path and isinstance(path["path"], str):
            return path["path"]

        if any(key in path for key in ("target", "origin")):
            return ""

    return str(path)


def decode_typed_value(value: Any) -> Any:
    if not isinstance(value, dict) or len(value) != 1:
        return value

    key, content = next(iter(value.items()))
    if key in {"jsonIetfVal", "jsonVal"}:
        if isinstance(content, bytes):
            try:
                return json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content

    # Not a gNMI typed-value wrapper, keep original object structure.
    return value


def _decode_json_like(content: Any) -> Any:
    if isinstance(content, bytes):
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return content.decode("utf-8", errors="replace")
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def decode_proto_typed_value(value: Any) -> Any:
    has_field = getattr(value, "HasField", None)
    if not callable(has_field):
        return decode_typed_value(value)

    if value.HasField("json_ietf_val"):
        return _decode_json_like(value.json_ietf_val)
    if value.HasField("json_val"):
        return _decode_json_like(value.json_val)
    if value.HasField("string_val"):
        return value.string_val
    if value.HasField("int_val"):
        return value.int_val
    if value.HasField("uint_val"):
        return value.uint_val
    if value.HasField("bool_val"):
        return value.bool_val
    if value.HasField("float_val"):
        return value.float_val
    if value.HasField("double_val"):
        return value.double_val
    if value.HasField("ascii_val"):
        return value.ascii_val
    if value.HasField("bytes_val"):
        return value.bytes_val
    if value.HasField("proto_bytes"):
        return value.proto_bytes
    if value.HasField("decimal_val"):
        decimal_val = value.decimal_val
        return decimal_val.digits / (10**decimal_val.precision)
    if value.HasField("leaflist_val"):
        return [decode_proto_typed_value(element) for element in value.leaflist_val.element]
    if value.HasField("any_val"):
        return str(value.any_val)

    return None


def decode_legacy_value_field(value: Any) -> Any:
    # Deprecated gNMI Value field retained by some implementations.
    raw = getattr(value, "value", b"")
    encoding = int(getattr(value, "type", -1))

    if encoding in {0, 4}:  # JSON / JSON_IETF
        return _decode_json_like(raw)
    if encoding == 3:  # ASCII
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    return raw


def iter_leaf_updates(base_path: str, value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        if not value:
            yield (base_path, value)
            return
        for key, nested in value.items():
            child_path = join_xpath(base_path, str(key))
            yield from iter_leaf_updates(child_path, nested)
        return

    if isinstance(value, list):
        if not value:
            yield (base_path, value)
            return
        for index, nested in enumerate(value):
            child_path = join_xpath(base_path, str(index))
            yield from iter_leaf_updates(child_path, nested)
        return

    yield (base_path, value)


def build_subscription(config: AppConfig) -> dict[str, Any]:
    subscriptions: list[dict[str, Any]] = []
    for path in config.paths:
        item: dict[str, Any] = {
            "path": path,
            "mode": config.subscription_mode,
        }
        if config.subscription_mode == "sample":
            item["sample_interval"] = config.sample_interval
        subscriptions.append(item)

    request = {
        "subscription": subscriptions,
        "mode": "stream",
        "encoding": config.encoding,
    }
    LOGGER.debug("Built gNMI subscribe request: %s", request)
    return request


def extract_events(message: Any, default_target: str) -> Iterable[TelemetryEvent]:
    LOGGER.debug("Processing gNMI message: %s", message)

    if hasattr(message, "HasField"):
        if message.HasField("sync_response"):
            LOGGER.debug("Received sync_response protobuf message")
            return []

        if not message.HasField("update"):
            return []

        body = message.update
        prefix_msg = body.prefix if body.HasField("prefix") else None
        target = getattr(prefix_msg, "target", "") or default_target
        base_path = xpath(prefix_msg) if prefix_msg is not None else ""
        timestamp_ns = int(getattr(body, "timestamp", 0) or time.time_ns())

        events: list[TelemetryEvent] = []
        for item in body.update:
            leaf = xpath(item.path)
            full_path = join_xpath(base_path, leaf)

            value: Any = None
            if item.HasField("val"):
                value = decode_proto_typed_value(item.val)
            elif item.HasField("value"):
                value = decode_legacy_value_field(item.value)

            for event_path, event_value in iter_leaf_updates(full_path, value):
                events.append(
                    TelemetryEvent(
                        timestamp_ns=timestamp_ns,
                        target=target,
                        path=event_path,
                        value=event_value,
                        update_type="update",
                    )
                )

        for item in body.delete:
            leaf = xpath(item)
            full_path = join_xpath(base_path, leaf)
            events.append(
                TelemetryEvent(
                    timestamp_ns=timestamp_ns,
                    target=target,
                    path=full_path,
                    value="<deleted>",
                    update_type="delete",
                )
            )

        return events

    if not isinstance(message, dict):
        return []

    if "sync_response" in message:
        LOGGER.debug("Received sync_response message")
        return []

    body = message.get("update")
    if not isinstance(body, dict):
        return []

    prefix = body.get("prefix", {})
    target = prefix.get("target", default_target) if isinstance(prefix, dict) else default_target
    base_path = xpath(prefix)
    timestamp_ns = int(body.get("timestamp", time.time_ns()))

    events: list[TelemetryEvent] = []
    updates = body.get("update", [])
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            leaf = xpath(item.get("path", ""))
            full_path = join_xpath(base_path, leaf)
            raw_value = item.get("val")
            if raw_value is None and "value" in item:
                raw_value = item.get("value")
            value = decode_typed_value(raw_value)
            for event_path, event_value in iter_leaf_updates(full_path, value):
                events.append(
                    TelemetryEvent(
                        timestamp_ns=timestamp_ns,
                        target=target,
                        path=event_path,
                        value=event_value,
                        update_type="update",
                    )
                )

    deletes = body.get("delete", [])
    if isinstance(deletes, list):
        for item in deletes:
            leaf = xpath(item)
            full_path = join_xpath(base_path, leaf)
            events.append(
                TelemetryEvent(
                    timestamp_ns=timestamp_ns,
                    target=target,
                    path=full_path,
                    value="<deleted>",
                    update_type="delete",
                )
            )

    return events


def stream_worker(config: AppConfig, out: queue.Queue[StreamEvent], stop: threading.Event) -> None:
    target_host, target_port = config.target.split(":", maxsplit=1)

    out.put(StatusEvent(level="info", message=f"Connecting to {config.target}", at_ns=time.time_ns()))
    LOGGER.debug(
        "Starting stream worker target=%s username=%s insecure=%s skip_verify=%s encoding=%s mode=%s sample_interval=%s paths=%s",
        config.target,
        config.username,
        config.insecure,
        config.skip_verify,
        config.encoding,
        config.subscription_mode,
        config.sample_interval,
        config.paths,
    )

    subscribe_request = build_subscription(config)

    try:
        with gNMIclient(
            target=(target_host, int(target_port)),
            username=config.username,
            password=config.password,
            insecure=config.insecure,
            skip_verify=config.skip_verify,
        ) as client:
            LOGGER.debug("Connected to gNMI target, invoking subscribe")
            out.put(StatusEvent(level="ok", message="Connected. Subscribing to telemetry stream...", at_ns=time.time_ns()))

            for message in client.subscribe(subscribe=subscribe_request):
                if stop.is_set():
                    LOGGER.debug("Stop flag set, leaving stream loop")
                    break
                for event in extract_events(message, default_target=target_host):
                    LOGGER.debug(
                        "Extracted telemetry event target=%s path=%s update_type=%s",
                        event.target,
                        event.path,
                        event.update_type,
                    )
                    out.put(event)

    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Stream worker failed")
        out.put(StatusEvent(level="error", message=f"Stream stopped: {exc}", at_ns=time.time_ns()))
