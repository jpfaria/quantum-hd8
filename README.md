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
| `quantum-hd8 scene save NAME` | Save the current state as scene `NAME` (refuses to overwrite an existing scene unless `--overwrite`) |
| `quantum-hd8 meters [--once]` | Level meters, dBFS calibrated 18/09 (raw value shown alongside) |

### Values

The daemon keeps every value normalized 0..1. `set` takes human units for `linear` curves
(e.g. `line/chN/preampgain` in dB, `global/ledBrightness` 1..100) and for `fader` curves (every
`volume` and `auxN` send, `main/ch1/volume`): dB, e.g. `-6`, `-6dB`, `0`, `+3`, `-inf` (fader
bottom); `0/1` (`on`/`off`) for toggles (`on`/`off` is refused on any non-toggle path); and the
normalized 0..1 value for `exp` and everything else. Pass `--raw` to `set` to write a normalized
0..1 value directly for any curve, bypassing the human-unit conversion (`Client.set(path, value,
raw=True)` in the library). Text params (`string`/`color`, e.g. `line/chN/username`) are not
writable. Curves and ranges per path: `quantum_hd8/params.json`. Writing the value a parameter
already has is a no-op (the daemon does not echo it). Every write goes to
`~/.quantum-hd8/undo.jsonl`; `undo` restores the last entry and removes it only once the write is
confirmed (a failed undo keeps it).

`quantum-hd8 set line/ch30/aux13 -6` → `line/ch30/aux13 = -6.0 dB (0.593)` (human dB, then the raw
normalized echo -- fader output gets the `dB` suffix since, unlike `linear`, a bare number there
wouldn't say what unit it is).

#### Fader curve dB mapping (measured 19/09)

The `fader` curve's dB ↔ normalized mapping is now measured (`quantum_hd8/fader.py`,
`tests/fixtures/fader-curve.json`): a tone into `line/ch30` (USB 4), `line/ch30/aux13`'s send
varied across 17 points, aux13's calibrated meter read at each -- see the fixture for the method.
Piecewise-linear between those points; below the lowest measured point (0.05) the mapping
extrapolates that segment's slope and clamps at -96 dB (the param's own floor); 0 (fader bottom)
is always -inf. **Assumption, not separately measured:** this curve was measured on one send
(`line/ch30/aux13`) and is applied to every param whose `params.json` `curve` is `"fader"` --
they share the same XML curve name and the same daemon-reported range (-96..10 dB).

`scene load --keep-gains` waits for the daemon's post-recall PVs to settle (link quiet 300 ms, max
3 s) before comparing gains; channels it could not restore are listed on stderr and it exits 1.

`scene load` can change `global/*` params as a side effect of the recall — measured 18/09: loading
`MK300-FRFR` flipped `global/mixerMode` from "Mixer Bypass" to "Analog + ADAT", which changes
routing. Every `scene load` snapshots `global/*` before and warns on stderr about anything that
changed (compared with the same tolerance as write-echo matching, so float-rounding noise from the
daemon's re-sent PVs — e.g. `global/mainOutVolumeLink` read back as `0.3333` instead of the
original `0.3333333432674408` — is not reported as a change); `--keep-mode` writes
`global/mixerMode` back if the scene changed it.

`scene save NAME` sends `StorePreset` (measured 19/09 from a capture of the UC app saving a scene)
and waits for the daemon's `StoredPreset` confirmation, then refreshes `scene list`. It refuses to
overwrite a scene already in `scene list` unless you pass `--overwrite` — never overwrite a scene
without asking first.

### Exit codes

`0` ok · `1` runtime/device error (daemon not running, write not confirmed, no meters) ·
`2` usage/validation error (bad value, unknown path, readonly, missing subcommand).

### Re-amp outputs

CoreAudio outputs 11/12 (Reamp 1/2) are **not** directly controllable from the host: their source
selector (front-panel *Global Settings > Reamp Out*, currently ADAT 1/2 on this unit) cannot be
changed by `quantum-hd8`. To send audio to the re-amp outs, play it on USB 11/12 (CoreAudio
outputs 11/12) — verified live. The earlier claim that a mixer aux/ADAT bus also reaches Re-amp 1
was a measurement artifact (stale reading, two variables changed at once); re-measured 19/09,
signal sent only to `aux/ch1` (Out 3/4) or only to `aux/ch6` (ADAT 3/4) does **not** reach Re-amp 1
(noise floor). Whether the panel's selected ADAT bus (`aux/ch5` = ADAT 1/2, this unit's setting)
reaches the re-amp outs is untested. See [`docs/camada-b-reamp.md`](docs/camada-b-reamp.md) for
the measurement.

## Verified live vs not

| Feature | Status (19/09) |
|---|---|
| `state`, `dump`, `get` | verified live |
| `set` + `undo` on `global/ledBrightness` | verified live |
| `preamp`, `route` writes (idempotent / no-op path) | verified live |
| `preamp N gain` real change + `undo` (In 3, 20.3 / 21 dB) | verified live |
| Re-amp 1 output from USB 11 (Mixer Bypass and Analog + ADAT) | verified live (cable Re-amp 1 → In 3, 18/09 and 19/09) |
| `scene list` | verified live |
| `meters` stream (UDP, layout in/aux/main) | verified live; values calibrated to dBFS (18/09) |
| `scene load` (+ `--keep-gains`, `--keep-mode`) | verified live (MK300-FRFR; the scene switched Mixer Mode, `--keep-mode` restored it) |
| `scene save` (+ overwrite guard) | verified live (new scene `TESTE`, 19/09) |
| Re-amp reachable from a mixer aux (Out 3/4 or ADAT 3/4 bus) | disproven (19/09): neither reaches Re-amp 1 (noise floor); earlier "reaches at -40.4 dBFS" claim was a measurement artifact. ADAT 1/2 (this unit's panel setting) also does not reach it (19/09); front-panel selector itself still can't be changed from the host |
| Fader/send dB ↔ 0..1 mapping | measured 19/09 (`line/ch30/aux13`, `quantum_hd8/fader.py`); applied to every `fader`-curve param as an assumption, not separately measured per path |

Protocol notes, measured vs hypothesis: [`docs/protocol.md`](docs/protocol.md).

## Tests

```bash
PYTHONPATH=. python3 -m pytest -q     # offline, fixtures only
```

`tests/test_device.py` (marked `device`, read-only: connect + one `get`) touches the real interface
and is deselected by default; run it only by hand with UC running: `python3 -m pytest -m device`.
