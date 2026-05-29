# gNMI Telemetry TUI

A terminal UI application that subscribes to gNMI streaming telemetry, can select specific paths for granular monitoring with text based graphing.  

98% created using AI tools :) 

## Features

- gNMI `stream` subscriptions using `sample`, `on_change`, or `target_defined`
- Auto-updating table of latest values by telemetry path
- Live filtering by path, target, value, and key
- Event log for connection/status messages
- Detail pane for full JSON payload of selected row
- YAML config file support and CLI overrides

## Quick Start

1. Create and activate a Python virtual environment.
1. Install dependencies:

```bash
pip install -r requirements.txt
```

1. Run the app:

```bash
python -m gnmi_tui \
  --target 10.0.0.1:57400 \
  --username admin \
  --password admin \
  --path /interfaces/interface/state/counters \
  --debug
```

Press `q` to quit.
Press `c` to toggle compact view.
Press `f` to freeze or resume the main table.
Press `i` to toggle the info pane.
Press `/` to jump to the path filter.
Press `x` to clear selected monitor paths.

## TUI Filtering

- Type in filter boxes at the top of the app to filter rows in real time.
- `Filter path`: Matches substrings in telemetry path.
- `Filter target`: Matches substrings in target name.
- `Filter value`: Matches substrings in rendered value text.
- `Filter key`: Matches substrings in key values from path predicates.
- In `Filter key`, press `Enter` to open a selectable list of discovered keys.
- Press `Ctrl+L` to clear all filters.

## View Modes

- Press `c` to toggle compact table view.
- Compact view shows: timestamp, key value, path-after-key, and value.
- Press `c` again to return to full view.

## Monitor and Graph Mode

- Move to a table row and press `Space` to select or unselect that row's path for monitoring.
- Press `Enter` on the table to open monitor mode.
- Monitor mode opens as a focused dialog above the main UI.
- The dialog shows monitored path/key pairs and appends timestamp + key + path-after-key + value entries as updates arrive.
- Press `Esc` to close monitor mode.
- Selected monitor paths are marked with `*` and highlighted in the table.
- While one or more monitor paths are selected, incoming table updates are paused.
- Press `g` to select a path to graph. Press `g` again to close the graph window 


## Freeze Mode

- Press `f` to freeze the main table display.
- While frozen, new incoming rows are buffered instead of rendered.
- Press `f` again to resume and flush buffered rows into the table.

## Configuration File

You can place settings in a YAML file and pass it with `--config`:

```yaml
target: 10.0.0.1:57400
username: admin
password: admin
insecure: true
skip_verify: true
encoding: json_ietf
subscription_mode: sample
sample_interval: 30s
history_limit: 300
debug: true
log_file: ./gnmi_tui_debug.log
paths:
  - /interfaces/interface/state/counters
  - /interfaces/interface/state/admin-status
```

Run with:

```bash
python -m gnmi_tui --config config.yaml
```

## CLI Options

- `--target host:port`
- `--username USER`
- `--password PASS`
- `--path PATH` (repeatable)
- `--paths path1,path2,path3`
- `--subscription-mode sample|on_change|target_defined`
- `--sample-interval DURATION` (for example `30s`, `500ms`, `1m`)
- `--encoding json_ietf`
- `--history-limit N`
- `--insecure`
- `--skip-verify`
- `--debug`
- `--log-file PATH`

## Debug Logging

If the target rejects or ignores the subscription, run with debug logging enabled:

```bash
python -m gnmi_tui \
  --target 10.0.0.1:57400 \
  --username admin \
  --password admin \
  --path /interfaces/interface/state/counters \
  --debug \
  --log-file ./gnmi_tui_debug.log
```

When debug mode is enabled, the application writes verbose details to the log file, including:

- resolved runtime options except the password
- the exact gNMI subscribe request payload
- raw messages returned by `subscribe()`
- extracted telemetry events and stream exceptions

## Notes

- For production, avoid plaintext credentials in shell history.
- Some devices require valid TLS setup even with `skip_verify`.
- Paths and encoding support depend on your network OS and gNMI server.
