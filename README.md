# quantum-hd8

Control a PreSonus Quantum HD 8 without the Universal Control window: UCNet client, CLI, Claude Code skill.

It talks to `ucdaemon` (the Universal Control background daemon) on TCP `127.0.0.1:59791`, the
same socket the UC app uses. The UC window can stay open and mirrors every change.

## Install

```bash
pipx install git+https://github.com/jpfaria/quantum-hd8
```

Claude Code plugin (skill `quantum-hd8`): add this repo as a marketplace
(`/plugin marketplace add jpfaria/quantum-hd8`) and install `quantum-hd8`.

## Requirements

- macOS, Python ≥ 3.11, no third-party runtime dependencies.
- PreSonus **Universal Control installed** and `ucdaemon` running (it starts with the app). The
  CLI never starts, stops or restarts the daemon.
- A Quantum HD 8 connected (firmware seen: 771).

## Commands

| Command | What it does |
|---|---|
| `quantum-hd8 state` | Readable summary (preamps, routes, scenes) |
| `quantum-hd8 dump [--out FILE]` | Every parameter path → normalized value, JSON |
| `quantum-hd8 get PATH` | One value (normalized 0..1 or string) |
| `quantum-hd8 listen [--raw FILE]` | Print change events until Ctrl-C (`--raw` saves the socket bytes) |
| `quantum-hd8 set PATH VALUE` | Write a parameter (units below), journaled for undo |
| `quantum-hd8 undo` | Restore the value before the last write (LIFO) |
| `quantum-hd8 preamp N gain DB` | Preamp gain of analog input 1-8, in dB |
| `quantum-hd8 preamp N phantom\|pad\|hpf on\|off` | Input toggles |
| `quantum-hd8 route phones1\|phones2\|spdif SOURCE` | Source by label (`"Loopback 1"`) or 0-based index |
| `quantum-hd8 scene list` | Scenes stored by UC |
| `quantum-hd8 scene load NAME [--keep-gains] [--keep-mode]` | Load a scene; `--keep-gains` re-applies the preamp gains it zeroes, `--keep-mode` re-applies the Mixer Mode if the scene changed it |
| `quantum-hd8 scene save NAME` | Not implemented (format not captured yet); exits 2 |
| `quantum-hd8 meters [--once]` | Level meters, dBFS calibrated 18/09 (raw value shown alongside) |

### Values

The daemon keeps every value normalized 0..1. `set` takes human units only for `linear` curves
(e.g. `line/chN/preampgain` in dB, `global/ledBrightness` 1..100), `0/1` (`on`/`off`) for toggles
(`on`/`off` is refused on any non-toggle path), and the normalized 0..1 value for `fader`/`exp` and
everything else. Text params (`string`/`color`, e.g. `line/chN/username`) are not writable. Curves and ranges per path:
`quantum_hd8/params.json`. Writing the value a parameter already has is a no-op (the daemon does
not echo it). Every write goes to `~/.quantum-hd8/undo.jsonl`; `undo` restores the last entry and
removes it only once the write is confirmed (a failed undo keeps it).

`scene load --keep-gains` waits for the daemon's post-recall PVs to settle (link quiet 300 ms, max
3 s) before comparing gains; channels it could not restore are listed on stderr and it exits 1.

`scene load` can change `global/*` params as a side effect of the recall — measured 18/09: loading
`MK300-FRFR` flipped `global/mixerMode` from "Mixer Bypass" to "Analog + ADAT", which changes
routing. Every `scene load` snapshots `global/*` before and warns on stderr about anything that
changed; `--keep-mode` writes `global/mixerMode` back if the scene changed it.

### Exit codes

`0` ok · `1` runtime/device error (daemon not running, write not confirmed, no meters) ·
`2` usage/validation error (bad value, unknown path, readonly, missing subcommand).

### Re-amp outputs

CoreAudio outputs 11/12 (Reamp 1/2) are **not** controllable from the host: their source is the
front-panel setting *Global Settings > Reamp Out* (ADAT 1/2 … ADAT 15/16). See
[`docs/camada-b-reamp.md`](docs/camada-b-reamp.md).

## Verified live vs not

| Feature | Status (18/09) |
|---|---|
| `state`, `dump`, `get` | verified live |
| `set` + `undo` on `global/ledBrightness` | verified live |
| `preamp`, `route` writes (idempotent / no-op path) | verified live |
| `scene list` | verified live |
| `meters` stream (UDP, layout in/aux/main) | verified live; values calibrated to dBFS (18/09) |
| `scene load` (+ `--keep-gains`, `--keep-mode`) | not yet live (built from the UC capture) |
| `scene save` | not captured, not implemented |
| Re-amp source | not reachable from the host (front panel only) |

Protocol notes, measured vs hypothesis: [`docs/protocol.md`](docs/protocol.md).

## Tests

```bash
PYTHONPATH=. python3 -m pytest -q     # offline, fixtures only
```

`tests/test_device.py` (marked `device`, read-only: connect + one `get`) touches the real interface
and is deselected by default; run it only by hand with UC running: `python3 -m pytest -m device`.
