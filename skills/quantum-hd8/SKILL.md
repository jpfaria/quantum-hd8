---
name: quantum-hd8
description: Use when the user wants to read or change anything on the PreSonus Quantum HD 8 without the Universal Control window — preamp gain, phantom, pad, HPF, phones/S-PDIF source, scenes, levels, any mixer or global parameter — e.g. "muda o ganho do canal 3 na HD 8", "carrega a cena X", "qual a fonte do fone", "medidores da interface", "roteia o S/PDIF", "liga o phantom do 2", "desfaz", or asks about the re-amp outputs (11/12) of the HD 8.
---

# quantum-hd8 — Quantum HD 8 via the Universal Control daemon

CLI + Python package that talks UCNet to `ucdaemon` (TCP `127.0.0.1:59791`), the same socket the
Universal Control (UC) app uses. The UC window can stay open; it mirrors every change.
Not installed? `pipx install git+https://github.com/jpfaria/quantum-hd8`
(or `pip install -e "${CLAUDE_PLUGIN_ROOT}"`). `quantum-hd8 <cmd> --help` for arguments.
Protocol details and what is measured vs hypothesis: `docs/protocol.md` in the repo.

**Requires the UC app installed (ucdaemon running).** Connection refused = UC not installed or
daemon not up: tell the user. **Never** restart/stop `ucdaemon`, never `launchctl`, never kill UC.

## Commands

| Want | Command |
|---|---|
| Readable summary | `quantum-hd8 state` |
| Everything (path → value) | `quantum-hd8 dump [--out f.json]` |
| One value | `quantum-hd8 get line/ch3/preampgain` |
| Preamp 1-8 | `quantum-hd8 preamp 3 gain 30` · `preamp 2 phantom on` · `pad`/`hpf on\|off` |
| Phones / S/PDIF source | `quantum-hd8 route phones1 "Loopback 1"` (`phones2`, `spdif`; label or 0-based index) |
| Any parameter | `quantum-hd8 set <path> <value>` |
| Revert last write | `quantum-hd8 undo` (LIFO, repeat for older ones) |
| Scenes | `quantum-hd8 scene list` · `scene load NAME --keep-gains --keep-mode` · `scene save NAME` |
| Levels (dBFS) | `quantum-hd8 meters --once` (JSON) or `meters` (live) |
| Watch changes | `quantum-hd8 listen` |

## Path map (measured: names from the device's `Synchronize`, fields from `params.json`)

| Path | Channel |
|---|---|
| `line/ch1..8` | In 1..8 (analog, the only real preamps) |
| `line/ch9..10` | S/PDIF 1..2 |
| `line/ch11..26` | ADAT 1..16 (`ch11+k-1` = ADAT k) |
| `line/ch27..36` | USB 1..10, DAW playback (`ch26+k` = USB k) |
| `aux/ch1..4` | Out 3/4, 5/6, 7/8, 9/10 |
| `aux/ch5..12` | ADAT out 1/2, 3/4 … 15/16 |
| `aux/ch13..14` | Loopback 1, Loopback 2 |
| `main/ch1` | Main L/R |

Leaves: every `line/chN` has `volume`, `mute`, `pan`, `aux1..aux14` (send to `aux/ch1..14`);
`aux/chN` and `main/ch1` have `volume`, `mute`. Preamp leaves of In 1..8 are
`preampgain` (dB 0..75), **`48v`** (not `phantom`), `pad`, `hpf`, `trim` (dB -12..12).
The CLI verb `preamp N phantom` writes `line/chN/48v`; for `get` use the path.
Example: "send do USB 3 pro aux 1" = `line/ch29/aux1`; "phantom do 2" = `line/ch2/48v`.
Exact path unknown? `quantum-hd8 dump | grep` it; never invent one.

## Values (most common mistake)

The daemon stores every value **normalized 0..1**. `set` converts only some:
- curve `linear` → **human units** (`line/chN/preampgain` in dB, `global/ledBrightness` 1..100).
- curve `fader` (every `volume` and `auxN` send, `main/ch1/volume`) → **dB**, measured 19/09
  (`quantum_hd8/fader.py`, piecewise-linear over the measured curve, `-96..+10`): `set
  line/ch29/aux1 -6`, `-6dB`, `0`, `+3`, `-inf` (fader bottom) all work now. **Assumption, not
  separately measured per path:** the curve was measured on one send (`line/ch30/aux13`) and
  applied to every `fader`-curve param, since they share the XML curve name and range.
  0.735 is seen on many faders at unity (0 dB) — that's now what `fader_db(0.735)` returns, not a
  guess.
- toggles → `0/1` (`on`/`off`).
- `exp` and anything else → **raw normalized 0..1**.
- `--raw` on `set` (or `Client.set(path, value, raw=True)`) writes a **normalized 0..1** value
  directly for any curve, bypassing the human/dB conversion — use it when you already have the
  normalized number (e.g. from `get`) and don't want it reinterpreted.
- Which curve a path has: `quantum_hd8/params.json` (fields `curve`, `type`, `min`, `max`,
  `units`), or use a shortcut (`preamp`, `route`) that converts.
- `get`, `dump` and `listen` always print the **normalized** value (0.192, not 20). Convert with
  min/max for linear params, or `quantum_hd8.fader.fader_db()` for fader params, before telling
  the user a dB number. `state` already prints `main/ch1` volume in dB.
- `route` and `state` print labels (`Loopback 1`), not the normalized index.
- Output of `set`: `global/ledBrightness = 20 (0.192)` for linear (human value, then the
  normalized echo); `line/ch30/aux13 = -6.0 dB (0.593)` for fader (dB, explicitly labeled, then
  the normalized echo); other params print the normalized echo only.
- Writing the value it already has is a **no-op**: the daemon sends no echo, the CLI returns the
  current value. Not an error, not a failed write.

## Rules

1. **Read before, write, read after.** `get` the path, write, `get` again and report both values.
2. **One new parameter at a time.** Written live so far: `global/ledBrightness`, preamp and route
   paths (the `verified` flag in `params.json` is still false everywhere, don't trust it). Any
   other path is written alone, re-read, confirmed with the user, before the next.
3. **Amps/cabinets:** before any write that can raise level on an output feeding the SYN-5050 /
   cabinets (main/aux volumes, routes, scene load, mutes off), ask the user to turn the volume down
   and wait for the "ok". Which outputs feed what: vault `music-setup — Mapa de Canais e Cabos`.
4. **Undo:** every `set`/`preamp`/`route` write is journaled (`~/.quantum-hd8/undo.jsonl`).
   Tell the user `quantum-hd8 undo` reverts it. `scene load` itself is not undoable.
5. **Scene load zeroes the preamp gains and can change Mixer Mode.** Loading a scene both zeroes
   preamp gains AND can change `global/mixerMode` (measured 18/09: MK300-FRFR flipped it from
   "Mixer Bypass" to "Analog + ADAT", which changes routing). Always
   `scene load NAME --keep-gains --keep-mode` unless the user explicitly wants the scene's gains
   or mode; it prints each gain and the mode it restored, and warns on stderr about any other
   `global/*` param the recall changed. Not yet verified live: re-read `state` after it.
   `scene save NAME` saves the current state as a scene. **Never overwrite a user's scene without
   explicit ask:** it refuses (exit 2) when `NAME` already exists in `scene list`, unless
   `--overwrite` is passed -- confirm with the user before adding `--overwrite`. Not yet verified
   live by this tool (built from a UC capture, 19/09).
6. **Meters are dBFS, calibrated 18/09** (peak, not RMS): `dBFS = 20·log10(raw / 65535)` (raw 0 =
   silence = -inf). `quantum-hd8 meters`/`meters --once` show both the raw value and the dBFS.
7. **Re-amp outs (CoreAudio 11/12): to send audio there, play it on USB 11/12.** *Global Settings
   > Reamp Out* (ADAT 1/2 … ADAT 15/16, currently ADAT 1/2 on this unit) can't be set/read by
   `set`/`route` — tell the user to change it on the panel. Do **not** claim a mixer aux/bus
   reaches the re-amp outs: re-measured 19/09 (median over 1.5 s, one variable at a time), signal
   sent only to `aux/ch1` (Out 3/4) or only to `aux/ch6` (ADAT 3/4) does **not** reach Re-amp 1
   (noise floor) — the earlier "reaches at −40.4 dBFS" claim was a stale-reading artifact. The
   panel's selected bus was tested too (19/09): signal only on `aux/ch5` (ADAT 1/2, −26 dBFS)
   does **not** reach Re-amp 1 either. No mixer bus feeds the re-amp outs while the host is
   connected; the Reamp Out selector's effect (standalone? ADAT input?) is unmeasured. `aux/ch13-14` are Loopback 1/2, unrelated to re-amp.
   Details: `docs/camada-b-reamp.md`.
8. No raw frames, no `tools/probe.py` writes, no guessing paths: paths come from `dump`.

## Red flags — stop

- About to pass dB/percent to an `exp` path (still raw normalized 0..1), or converting dB to 0..1
  yourself for a `linear`/`fader` path instead of letting `set` do it (or `--raw` when you
  already have the normalized value).
- Using `phantom` in a path, or a path not in the map / `dump`.
- Reporting a meter value as RMS, or without saying it's a peak reading.
- `scene load` without `--keep-gains --keep-mode` and without the user asking for the scene's
  gains/mode.
- Hunting for a re-amp parameter, or proposing `launchctl`/restarting ucdaemon.
- Several unverified writes in one command line, or raising output level without asking first.
