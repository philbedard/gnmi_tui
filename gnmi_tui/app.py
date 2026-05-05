from __future__ import annotations

import queue
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Log, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .config import AppConfig
from .formatting import format_value, render_detail
from .stream import StatusEvent, StreamEvent, TelemetryEvent, stream_worker


@dataclass(slots=True)
class RowState:
    timestamp: str
    target: str
    path: str
    value: Any
    update_type: str


@dataclass(slots=True)
class BufferedRow:
    row_key: str
    row: RowState


class HelpScreen(Screen[None]):
    """Help dialog showing all keybindings."""
    
    CSS = """
    Screen {
        align: center middle;
    }
    
    #help-dialog {
        width: 70;
        height: auto;
        border: round rgb(110, 110, 110);
        background: rgb(12, 12, 12);
        padding: 1;
    }
    
    #help-content {
        height: auto;
        border: none;
    }
    """
    
    def compose(self) -> ComposeResult:
        help_text = Text()
        help_text.append("Keybindings\n\n", style="bold cyan")
        help_text.append("?          ", style="bold yellow")
        help_text.append("Show this help dialog\n")
        help_text.append("/          ", style="bold yellow")
        help_text.append("Focus path filter\n")
        help_text.append("i          ", style="bold yellow")
        help_text.append("Toggle info pane\n")
        help_text.append("c          ", style="bold yellow")
        help_text.append("Toggle compact mode\n")
        help_text.append("f          ", style="bold yellow")
        help_text.append("Freeze/unfreeze display\n")
        help_text.append("space      ", style="bold yellow")
        help_text.append("Select/deselect path for monitoring\n")
        help_text.append("enter      ", style="bold yellow")
        help_text.append("Open monitor mode for selected paths\n")
        help_text.append("g          ", style="bold yellow")
        help_text.append("Graph selected numeric monitor path\n")
        help_text.append("x          ", style="bold yellow")
        help_text.append("Clear all selected paths\n")
        help_text.append("q          ", style="bold yellow")
        help_text.append("Quit\n\n")
        help_text.append("Press any key to close", style="dim")
        
        with Vertical(id="help-dialog"):
            yield Static(help_text, id="help-content")
    
    def on_key(self, event: Key) -> None:
        self.app.pop_screen()


class TelemetryTUI(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #status {
        height: 1;
        padding: 0 1;
        background: rgb(20, 20, 20);
        color: white;
    }

    #main {
        height: 1fr;
    }

    #filters {
        height: auto;
        padding: 0 1;
        background: rgb(32, 32, 32);
    }

    #filters Input {
        width: 1fr;
        margin: 0 1 0 0;
    }

    #key-picker {
        display: none;
        height: 8;
        margin: 0 1;
    }

    #table-pane {
        width: 2fr;
    }

    #detail-pane {
        width: 1fr;
    }

    #detail {
        border: round rgb(90, 90, 90);
        padding: 1;
        height: 1fr;
    }

    #events-log {
        height: 12;
    }

    #monitor-dialog {
        display: none;
        layer: overlay;
        width: 92%;
        height: 80%;
        border: round rgb(110, 110, 110);
        background: rgb(12, 12, 12);
        padding: 1;
    }

    #monitor-title {
        height: 1;
    }

    #monitor-paths {
        height: auto;
        border: round rgb(70, 70, 70);
        padding: 0 1;
        margin: 1 0;
    }

    #monitor-body {
        height: 1fr;
    }

    #monitor-log {
        height: 1fr;
        border: round rgb(70, 70, 70);
    }

    #monitor-graph-picker {
        display: none;
        height: 8;
        border: round rgb(70, 70, 70);
        margin: 1 0 0 0;
    }

    #monitor-graph {
        display: none;
        width: 1fr;
        height: 12;
        border: round rgb(70, 70, 70);
        padding: 0 1;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        ("?", "show_help", "Help"),
        ("/", "focus_filter_path", "Filter Path"),
        ("i", "toggle_info_pane", "Info Pane"),
        ("c", "toggle_compact", "Compact"),
        ("f", "toggle_freeze", "Freeze"),
        ("space", "toggle_monitor_select", "Select Path"),
        ("enter", "open_monitor_selected", "Monitor"),
        ("x", "clear_monitor_selection", "Clear"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._queue: queue.Queue[StreamEvent] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._latest: "OrderedDict[str, RowState]" = OrderedDict()
        self._row_seq = 0
        self._status = "Idle"
        self._event_count = 0
        self._visible_count = 0
        self._info_pane_visible = False
        self._compact_mode = False
        self._display_frozen = False
        self._monitor_paths: set[str] = set()
        self._monitor_open = False
        self._pending_rows: list[BufferedRow] = []
        self._graph_enabled = False
        self._graph_path: str | None = None
        self._graph_points: list[float] = []
        self._graph_limit = 90
        self._monitor_path_line_count = 1

        self._path_filter = ""
        self._target_filter = ""
        self._value_filter = ""
        self._key_filter = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("status", id="status")
        with Horizontal(id="filters"):
            yield Input(placeholder="Filter path", id="filter-path")
            yield Input(placeholder="Filter target", id="filter-target")
            yield Input(placeholder="Filter value", id="filter-value")
            yield Input(placeholder="Filter key (press Enter for list)", id="filter-key")
        yield OptionList(id="key-picker")
        with Horizontal(id="main"):
            with Vertical(id="table-pane"):
                yield DataTable(id="telemetry-table")
                yield Log(id="events-log", highlight=True)
            with Vertical(id="detail-pane"):
                yield Static("Select a telemetry row to view full payload", id="detail")
        with Vertical(id="monitor-dialog"):
            yield Static("Monitor Mode", id="monitor-title")
            yield Static("No monitored paths selected", id="monitor-paths")
            with Vertical(id="monitor-body"):
                yield RichLog(id="monitor-log", highlight=False, markup=False, wrap=False)
                yield OptionList(id="monitor-graph-picker")
                yield Static("Graph inactive (press g)", id="monitor-graph")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#telemetry-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        self._configure_table_columns()
        self._apply_info_pane_visibility()
        table.focus()

        self._thread = threading.Thread(
            target=stream_worker,
            args=(self.config, self._queue, self._stop),
            daemon=True,
            name="gnmi-stream-worker",
        )
        self._thread.start()

        self.set_interval(0.2, self._drain_events)

    def on_unmount(self) -> None:
        self._stop.set()

    def action_refresh(self) -> None:
        self._render_table()
        self._refresh_status()

    def action_show_help(self) -> None:
        """Show the help dialog with all keybindings."""
        self.push_screen(HelpScreen())

    def action_focus_filter_path(self) -> None:
        filter_path = self.query_one("#filter-path", Input)
        filter_path.focus()

    def action_toggle_info_pane(self) -> None:
        self._info_pane_visible = not self._info_pane_visible
        self._apply_info_pane_visibility()

    def _apply_info_pane_visibility(self) -> None:
        detail_pane = self.query_one("#detail-pane", Vertical)
        table_pane = self.query_one("#table-pane", Vertical)

        if self._info_pane_visible:
            detail_pane.display = True
            table_pane.styles.width = "2fr"
        else:
            detail_pane.display = False
            table_pane.styles.width = "1fr"

    def action_toggle_compact(self) -> None:
        self._compact_mode = not self._compact_mode
        self._configure_table_columns()
        self._render_table()
        self._refresh_status()

    def action_toggle_freeze(self) -> None:
        self._display_frozen = not self._display_frozen
        if not self._is_table_paused():
            self._flush_pending_rows()
        self._render_table()
        self._refresh_status()

    def action_toggle_monitor_select(self) -> None:
        if self._monitor_open:
            return
        if not isinstance(self.focused, DataTable):
            return
        self._toggle_monitor_path_for_current_row()

    def action_open_monitor_selected(self) -> None:
        if self._monitor_open:
            return
        if not isinstance(self.focused, DataTable):
            return
        self._open_monitor_dialog()

    def action_toggle_monitor_graph(self) -> None:
        if not self._monitor_open:
            return
        self._toggle_monitor_graph_picker()

    def action_clear_monitor_selection(self) -> None:
        if not self._monitor_paths:
            return

        self._monitor_paths.clear()
        self._hide_monitor_graph_picker(refocus=False)
        self._graph_enabled = False
        self._graph_path = None
        self._graph_points.clear()
        self._apply_monitor_dialog_height()
        if not self._is_table_paused():
            self._flush_pending_rows()
        if self._monitor_open:
            self._refresh_monitor_header()
            self._refresh_monitor_graph()
        self._render_table()
        self._refresh_status()

    def action_clear_filters(self) -> None:
        self.query_one("#filter-path", Input).value = ""
        self.query_one("#filter-target", Input).value = ""
        self.query_one("#filter-value", Input).value = ""
        self.query_one("#filter-key", Input).value = ""

        self._path_filter = ""
        self._target_filter = ""
        self._value_filter = ""
        self._key_filter = ""

        self._hide_key_picker()

        self._render_table()
        self._refresh_status()

    @on(Input.Changed, "#filter-path")
    def on_filter_path_changed(self, event: Input.Changed) -> None:
        self._path_filter = event.value.strip().lower()
        self._render_table()
        self._refresh_status()

    @on(Input.Changed, "#filter-target")
    def on_filter_target_changed(self, event: Input.Changed) -> None:
        self._target_filter = event.value.strip().lower()
        self._render_table()
        self._refresh_status()

    @on(Input.Changed, "#filter-value")
    def on_filter_value_changed(self, event: Input.Changed) -> None:
        self._value_filter = event.value.strip().lower()
        self._render_table()
        self._refresh_status()

    @on(Input.Changed, "#filter-key")
    def on_filter_key_changed(self, event: Input.Changed) -> None:
        self._key_filter = event.value.strip().lower()
        self._render_table()
        self._refresh_status()

    @on(Input.Submitted, "#filter-key")
    def on_filter_key_submitted(self, event: Input.Submitted) -> None:
        partial = event.value.strip().lower()
        options = self._available_key_values(partial)
        if not options:
            self._hide_key_picker()
            return

        picker = self.query_one("#key-picker", OptionList)
        picker.clear_options()
        picker.add_options([Option(value, id=value) for value in options])
        picker.display = True
        picker.focus()

    @on(OptionList.OptionSelected, "#key-picker")
    def on_key_option_selected(self, event: OptionList.OptionSelected) -> None:
        selected = event.option.id if event.option.id is not None else str(event.option.prompt)
        key_input = self.query_one("#filter-key", Input)
        key_input.value = selected
        self._key_filter = selected.strip().lower()
        self._hide_key_picker()
        key_input.focus()
        self._render_table()
        self._refresh_status()

    @on(OptionList.OptionSelected, "#monitor-graph-picker")
    def on_monitor_graph_option_selected(self, event: OptionList.OptionSelected) -> None:
        selected_path = event.option.id if event.option.id is not None else str(event.option.prompt)
        if selected_path not in self._monitor_paths:
            self._hide_monitor_graph_picker(refocus=True)
            return

        if self._graph_enabled and self._graph_path == selected_path:
            self._graph_enabled = False
            self._graph_path = None
            self._graph_points.clear()
        else:
            self._graph_enabled = True
            self._graph_path = selected_path
            self._graph_points = self._seed_graph_points(selected_path)

        self._hide_monitor_graph_picker(refocus=True)
        self._apply_monitor_dialog_height()
        self._refresh_monitor_graph()
        self._refresh_status()

    def on_key(self, event: Key) -> None:
        if event.key == "g" and self._monitor_open:
            self.action_toggle_monitor_graph()
            event.stop()
            return

        if event.key == "escape":
            if self._monitor_open:
                picker = self.query_one("#monitor-graph-picker", OptionList)
                if picker.display:
                    self._hide_monitor_graph_picker(refocus=True)
                    event.stop()
                    return
                self._close_monitor_dialog()
                event.stop()
                return

            picker = self.query_one("#key-picker", OptionList)
            if picker.display:
                self._hide_key_picker()
                self.query_one("#filter-key", Input).focus()
                event.stop()
                return

    @on(DataTable.RowHighlighted, "#telemetry-table")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        row_key = str(event.row_key.value)
        row = self._latest.get(row_key)
        if row is None:
            return
        detail = self.query_one("#detail", Static)
        detail.update(Text(
            "\n".join(
                [
                    f"Path: {row.path}",
                    f"Target: {row.target}",
                    f"Timestamp: {row.timestamp}",
                    f"Type: {row.update_type}",
                    "",
                    render_detail(row.value),
                ]
            )
        ))

    @on(DataTable.RowSelected, "#telemetry-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable may consume Enter before app-level bindings.
        # Opening monitor mode here keeps Enter behavior reliable.
        _ = event
        if self._monitor_open:
            return
        self._open_monitor_dialog()

    def _drain_events(self) -> None:
        changed = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(item, StatusEvent):
                self._status = item.message
                self.query_one("#events-log", Log).write_line(f"{item.level}: {escape(item.message)}")
                changed = True
                continue

            if isinstance(item, TelemetryEvent):
                self._event_count += 1
                self._row_seq += 1
                row = RowState(
                    timestamp=item.iso_time,
                    target=item.target,
                    path=item.path,
                    value=item.value,
                    update_type=item.update_type,
                )
                row_key = f"{item.timestamp_ns}:{self._row_seq}"

                if self._monitor_open and item.path in self._monitor_paths:
                    monitor_log = self.query_one("#monitor-log", RichLog)
                    monitor_log.write(self._format_monitor_line(row))
                    if self._graph_enabled and self._graph_path == item.path:
                        numeric = self._to_numeric(row.value)
                        if numeric is not None:
                            self._graph_points.append(numeric)
                            if len(self._graph_points) > self._graph_limit:
                                self._graph_points = self._graph_points[-self._graph_limit :]
                            self._refresh_monitor_graph()

                if self._is_table_paused():
                    self._pending_rows.append(BufferedRow(row_key=row_key, row=row))
                    changed = True
                    continue

                self._append_row(row_key, row)

                changed = True

        if changed:
            self._render_table()
            self._refresh_status()

    def _refresh_status(self) -> None:
        status = self.query_one("#status", Static)
        view = "compact" if self._compact_mode else "full"
        monitor_info = f"Monitor: {len(self._monitor_paths)} path(s)"
        if self._graph_enabled and self._graph_path:
            monitor_info += f" | Graph: {self._path_after_last_key(self._graph_path)}"
        if self._display_frozen:
            paused_info = f"Updates: frozen ({len(self._pending_rows)} buffered)"
        elif self._monitor_paths:
            paused_info = f"Updates: paused ({len(self._pending_rows)} buffered)"
        else:
            paused_info = "Updates: live"
        status.update(Text(
            f"Target: {self.config.target} | Paths: {len(self.config.paths)} | "
            f"Rows: {self._visible_count}/{len(self._latest)} | View: {view} | {monitor_info} | {paused_info} | Events: {self._event_count} | {self._status}"
        ))

    def _configure_table_columns(self) -> None:
        table = self.query_one("#telemetry-table", DataTable)
        table.clear(columns=True)
        if self._compact_mode:
            table.add_columns("*", "Timestamp (UTC)", "Key", "Path Tail", "Value")
            return
        table.add_columns("*", "Timestamp (UTC)", "Target", "Type", "Path", "Value")

    def _is_table_paused(self) -> bool:
        return self._display_frozen or bool(self._monitor_paths)

    def _append_row(self, row_key: str, row: RowState) -> None:
        self._latest[row_key] = row
        self._latest.move_to_end(row_key)

    def _flush_pending_rows(self) -> None:
        if not self._pending_rows:
            return
        for pending in self._pending_rows:
            self._append_row(pending.row_key, pending.row)
        self._pending_rows.clear()

    def _current_table_row(self) -> RowState | None:
        table = self.query_one("#telemetry-table", DataTable)
        if not self._latest or table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            row_key = str(cell_key.row_key.value)
        except Exception:
            return None
        return self._latest.get(row_key)

    def _toggle_monitor_path_for_current_row(self) -> None:
        row = self._current_table_row()
        if row is None:
            return

        path = row.path
        if path in self._monitor_paths:
            self._monitor_paths.remove(path)
            if self._graph_path == path:
                self._hide_monitor_graph_picker(refocus=False)
                self._graph_enabled = False
                self._graph_path = None
                self._graph_points.clear()
                self._apply_monitor_dialog_height()
        else:
            self._monitor_paths.add(path)
        if not self._is_table_paused():
            self._flush_pending_rows()
        if self._monitor_open:
            self._refresh_monitor_header()
            self._refresh_monitor_graph()
        self._render_table()
        self._refresh_status()

    def _refresh_monitor_header(self) -> None:
        title = self.query_one("#monitor-title", Static)
        title.update(Text(f"Monitor Mode ({len(self._monitor_paths)} path(s)) - g: select graph path - Esc: close"))

        paths_view = self.query_one("#monitor-paths", Static)
        if not self._monitor_paths:
            paths_view.update(Text("No monitored paths selected"))
            paths_view.styles.height = 3
            self._monitor_path_line_count = 1
            self._apply_monitor_dialog_height()
            return

        lines = ["Monitored paths:"]
        for path in sorted(self._monitor_paths):
            lines.append(f"- key={self._path_key_value(path)} path={path}")
        paths_view.update(Text("\n".join(lines)))

        line_count = len(lines)
        self._monitor_path_line_count = line_count
        paths_view.styles.height = line_count + 2
        self._apply_monitor_dialog_height()

    def _apply_monitor_dialog_height(self) -> None:
        dialog = self.query_one("#monitor-dialog", Vertical)
        terminal_height = max(24, self.size.height)

        # Keep prior percentage-based behavior as baseline, then convert to lines.
        baseline_percent = min(95, max(60, 50 + (self._monitor_path_line_count * 3)))
        baseline_lines = int((terminal_height * baseline_percent) / 100)

        if self._graph_enabled:
            baseline_lines += 10

        baseline_lines = max(14, min(terminal_height - 2, baseline_lines))
        dialog.styles.height = baseline_lines

    def _monitor_column_widths(self) -> tuple[int, int, int]:
        if not self._monitor_paths:
            return (1, 1, 1)

        key_width = max(len(self._path_key_value(path)) for path in self._monitor_paths)
        path_width = max(len(self._path_after_last_key(path)) for path in self._monitor_paths)
        # Derive target width from latest known rows for monitored paths
        target_width = 1
        for row in self._latest.values():
            if row.path in self._monitor_paths:
                target_width = max(target_width, len(row.target))
        return (key_width, path_width, target_width)

    def _format_monitor_line(self, row: RowState) -> Text:
        key_width, path_width, target_width = self._monitor_column_widths()
        timestamp = row.timestamp.ljust(24)
        target = row.target.ljust(target_width)
        key = self._path_key_value(row.path).ljust(key_width)
        path = self._path_after_last_key(row.path).ljust(path_width)
        value = format_value(row.value, max_len=None)
        line = Text()
        line.append(timestamp, style="bold yellow")
        line.append(" | ", style="dim")
        line.append(target, style="bold blue")
        line.append(" | ", style="dim")
        line.append(key, style="magenta")
        line.append(" | ", style="dim")
        line.append(path, style="green")
        line.append(" | ", style="dim")
        line.append(value, style="cyan")
        return line

    def _open_monitor_dialog(self) -> None:
        if not self._monitor_paths:
            row = self._current_table_row()
            if row is not None:
                self._monitor_paths.add(row.path)

        if not self._monitor_paths:
            return

        self._monitor_open = True
        dialog = self.query_one("#monitor-dialog", Vertical)
        monitor_log = self.query_one("#monitor-log", RichLog)
        monitor_log.clear()
        self._hide_monitor_graph_picker(refocus=False)
        self._graph_enabled = False
        self._graph_path = None
        self._graph_points.clear()
        self._refresh_monitor_header()
        self._apply_monitor_dialog_height()
        self._refresh_monitor_graph()
        dialog.display = True
        monitor_log.focus()
        self._refresh_status()

    def _close_monitor_dialog(self) -> None:
        self._monitor_open = False
        dialog = self.query_one("#monitor-dialog", Vertical)
        dialog.display = False
        self._hide_monitor_graph_picker(refocus=False)
        self._graph_enabled = False
        self._graph_path = None
        self._graph_points.clear()
        self.query_one("#telemetry-table", DataTable).focus()
        self._refresh_status()

    def _toggle_monitor_graph_picker(self) -> None:
        if not self._monitor_paths:
            return

        picker = self.query_one("#monitor-graph-picker", OptionList)
        if picker.display:
            self._hide_monitor_graph_picker(refocus=True)
            return

        picker.clear_options()
        options: list[Option] = []
        for path in sorted(self._monitor_paths):
            key = self._path_key_value(path)
            tail = self._path_after_last_key(path)
            prompt = f"key={key} | {tail}"
            options.append(Option(prompt, id=path))

        picker.add_options(options)
        picker.display = True
        picker.focus()
        # Auto-select the first item so the highlight is immediately visible
        if options:
            picker.highlighted = 0

    def _hide_monitor_graph_picker(self, refocus: bool) -> None:
        picker = self.query_one("#monitor-graph-picker", OptionList)
        picker.clear_options()
        picker.display = False
        if refocus and self._monitor_open:
            self.query_one("#monitor-log", RichLog).focus()

    def _to_numeric(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return float(stripped)
            except ValueError:
                return None
        return None

    def _format_numeric_plain(self, value: float) -> str:
        rendered = f"{value:.6f}".rstrip("0").rstrip(".")
        if rendered == "-0":
            return "0"
        return rendered

    def _seed_graph_points(self, path: str) -> list[float]:
        points: list[float] = []
        for row in self._latest.values():
            if row.path != path:
                continue
            numeric = self._to_numeric(row.value)
            if numeric is None:
                continue
            points.append(numeric)
        if len(points) > self._graph_limit:
            return points[-self._graph_limit :]
        return points

    def _hash_bar_rows(self, points: list[float], width: int, rows: int) -> list[tuple[str, str]]:
        if width <= 0 or rows <= 0:
            return []
        if not points:
            return [("." * width, "n/a") for _ in range(rows)]

        values = points[-rows:]
        minimum = min(values)
        maximum = max(values)

        output: list[tuple[str, str]] = []
        for value in values:
            if maximum == minimum:
                bar_len = max(1, width // 2)
            else:
                normalized = (value - minimum) / (maximum - minimum)
                bar_len = max(1, int(normalized * width))
            output.append(("#" * bar_len + "." * (width - bar_len), self._format_numeric_plain(value)))

        if len(output) < rows:
            pad = [("." * width, "n/a") for _ in range(rows - len(output))]
            output = pad + output

        return output

    def _refresh_monitor_graph(self) -> None:
        graph = self.query_one("#monitor-graph", Static)
        if not self._graph_enabled or not self._graph_path:
            graph.display = False
            return

        terminal_height = max(24, self.size.height)
        desired_graph_height = max(9, min(22, terminal_height // 3))
        graph.styles.height = desired_graph_height
        graph.display = True

        path_tail = self._path_after_last_key(self._graph_path)
        key_value = self._path_key_value(self._graph_path)
        latest = self._graph_points[-1] if self._graph_points else None
        latest_text = self._format_numeric_plain(latest) if latest is not None else "n/a"
        graph_width = graph.size.width if graph.size.width > 0 else 70
        tick_space = 14
        bar_width = max(20, min(100, graph_width - tick_space))
        bar_rows = max(3, desired_graph_height - 7)
        bars = self._hash_bar_rows(self._graph_points, width=bar_width, rows=bar_rows)
        if self._graph_points:
            min_text = self._format_numeric_plain(min(self._graph_points))
            max_text = self._format_numeric_plain(max(self._graph_points))
        else:
            min_text = "n/a"
            max_text = "n/a"

        body = Text()
        body.append("Graph\n", style="bold cyan")
        body.append(f"key: {key_value}\n", style="magenta")
        body.append(f"path: {path_tail}\n", style="green")
        body.append(f"latest: {latest_text}\n", style="yellow")
        body.append(f"min/max: {min_text} / {max_text}\n", style="dim")
        body.append("newest: bottom row (top is oldest)\n", style="dim")
        for bar, tick in bars:
            body.append(bar, style="cyan")
            body.append("  ", style="dim")
            body.append(tick + "\n", style="bold white")
        body.append("\n")
        body.append("g toggles graph (monitor only)", style="dim")
        graph.update(body)

    def _path_tail(self, path: str) -> str:
        stripped = path.rstrip("/")
        if not stripped:
            return "/"
        return stripped.split("/")[-1]

    def _path_after_last_key(self, path: str) -> str:
        stripped = path.strip()
        if not stripped:
            return "/"

        matches = list(re.finditer(r"\[[^\]]+\]", stripped))
        if not matches:
            return stripped.strip("/") or "/"

        tail = stripped[matches[-1].end() :].lstrip("/")
        if tail:
            return tail
        return stripped.strip("/") or "/"

    def _path_key_value(self, path: str) -> str:
        matches = re.findall(r"\[([^=\]]+)=([^\]]+)\]", path)
        if not matches:
            return "-"
        _, value = matches[-1]
        return value

    def _path_key_values(self, path: str) -> list[str]:
        return [value for _, value in re.findall(r"\[([^=\]]+)=([^\]]+)\]", path)]

    def _available_key_values(self, partial: str) -> list[str]:
        seen: set[str] = set()
        values: list[str] = []
        for row in reversed(self._latest.values()):
            for value in self._path_key_values(row.path):
                if partial and partial not in value.lower():
                    continue
                if value in seen:
                    continue
                seen.add(value)
                values.append(value)
        return values

    def _hide_key_picker(self) -> None:
        picker = self.query_one("#key-picker", OptionList)
        picker.clear_options()
        picker.display = False

    def _matches_filters(self, row: RowState) -> bool:
        if self._path_filter and self._path_filter not in row.path.lower():
            return False
        if self._target_filter and self._target_filter not in row.target.lower():
            return False
        if self._key_filter:
            keys = self._path_key_values(row.path)
            if not any(self._key_filter in key.lower() for key in keys):
                return False
        if self._value_filter:
            rendered = format_value(row.value, max_len=400).lower()
            if self._value_filter not in rendered:
                return False
        return True

    def _render_table(self) -> None:
        table = self.query_one("#telemetry-table", DataTable)

        selected_row_key: str | None = None
        selected_column = 0
        if table.row_count > 0:
            try:
                cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
                selected_row_key = str(cell_key.row_key.value)
                selected_column = table.cursor_coordinate.column
            except Exception:
                selected_row_key = None

        table.clear()
        visible_rows = [(row_key, row) for row_key, row in reversed(self._latest.items()) if self._matches_filters(row)]
        visible = 0
        for row_key, row in visible_rows:
            marker = "*" if row.path in self._monitor_paths else ""
            style = "black on rgb(200, 140, 0)" if row.path in self._monitor_paths else ""
            value_style = "cyan" if row.path not in self._monitor_paths else "cyan on rgb(200, 140, 0)"
            if self._compact_mode:
                table.add_row(
                    Text(marker, style=style),
                    Text(row.timestamp, style=style),
                    Text(self._path_key_value(row.path), style=style),
                    Text(self._path_after_last_key(row.path), style=style),
                    Text(format_value(row.value), style=value_style),
                    key=row_key,
                )
            else:
                table.add_row(
                    Text(marker, style=style),
                    Text(row.timestamp, style=style),
                    Text(row.target, style=style),
                    Text(row.update_type, style=style),
                    Text(row.path, style=style),
                    Text(format_value(row.value), style=value_style),
                    key=row_key,
                )
            visible += 1
        self._visible_count = visible

        # Restore cursor position after render cycle completes
        if table.row_count > 0 and selected_row_key:
            self.call_later(self._restore_cursor, selected_row_key, selected_column)
    
    def _restore_cursor(self, row_key: str, column: int) -> None:
        """Restore cursor to a specific row and column."""
        try:
            table = self.query_one("#telemetry-table", DataTable)
            row_index = table.get_row_index(row_key)
            max_column = max(0, table.column_count - 1)
            table.move_cursor(row=row_index, column=min(column, max_column), animate=False, scroll=True)
        except Exception:
            pass
