import argparse
import difflib
import json
import os
import socket
import sys

from . import __version__
from . import undo
from .client import Client, SceneLoadTimeout, WriteNotConfirmed, default_udp_factory

METER_SECTIONS = ("in", "aux", "main")
METER_HEADER = "valores crus do daemon — escala não calibrada"
METER_MAX_CONSECUTIVE_TIMEOUTS = 5
METER_CLEAR_SCREEN = "\x1b[H\x1b[2J"

# Label lists for global/phones1_src, global/phones2_src and
# global/spdifSource, measured from tests/fixtures/uc-pl.bin (the daemon
# does not send PL at subscribe -- task-8-brief.md -- so these are the
# fallback when Client.lists has no live entry for the path). Verified
# against the fixture in tests/test_shortcuts.py.
_ROUTE_LABELS_15 = [
    "Main L/R", "Out  3/4", "Out  5/6", "Out  7/8", "Out  9/10",
    "ADAT  1/2", "ADAT  3/4", "ADAT  5/6", "ADAT  7/8", "ADAT  9/10",
    "ADAT  11/12", "ADAT  13/14", "ADAT  15/16", "Loopback  1", "Loopback  2",
]
STATIC_LABELS: dict[str, list[str]] = {
    "global/phones1_src": _ROUTE_LABELS_15,
    "global/phones2_src": _ROUTE_LABELS_15,
    "global/spdifSource": _ROUTE_LABELS_15 + ["S/PDIF Out"],
}

_ROUTE_TARGETS = {
    "phones1": "global/phones1_src",
    "phones2": "global/phones2_src",
    "spdif": "global/spdifSource",
}

_PREAMP_TOGGLE_PARAMS = {
    "phantom": "48v",
    "pad": "pad",
    "hpf": "hpf",
}


def _resolve_label_index(source: str, labels: list[str]) -> int | None:
    """`source` -> index into `labels`: a 0-based numeric index, or a label
    matched case-insensitively with whitespace collapsed (the device uses
    double spaces, e.g. "Out  3/4"; users may type "out 3/4"). None when it
    matches neither."""
    s = source.strip()
    if s.isdigit():
        idx = int(s)
        return idx if 0 <= idx < len(labels) else None
    norm = " ".join(s.split()).lower()
    for i, label in enumerate(labels):
        if " ".join(label.split()).lower() == norm:
            return i
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quantum-hd8")
    p.add_argument("--version", action="version", version=f"quantum-hd8 {__version__}")
    sub = p.add_subparsers(dest="cmd")

    dump = sub.add_parser("dump", help="Estado achatado em JSON")
    dump.add_argument("--out", metavar="ARQUIVO")

    get = sub.add_parser("get", help="Valor de um caminho")
    get.add_argument("path")

    sub.add_parser("state", help="Resumo legível do estado")

    listen = sub.add_parser("listen", help="Imprime eventos até Ctrl-C")
    listen.add_argument("--raw", metavar="ARQUIVO",
                         help="Grava cada chunk cru recebido do socket neste arquivo")

    set_p = sub.add_parser("set", help="Escreve um parâmetro")
    set_p.add_argument("path")
    set_p.add_argument("value")

    sub.add_parser("undo", help="Desfaz a última escrita")

    scene = sub.add_parser("scene", help="Cenas (presets)")
    scene_sub = scene.add_subparsers(dest="scene_cmd")
    scene_sub.add_parser("list", help="Lista as cenas")
    scene_load = scene_sub.add_parser("load", help="Carrega uma cena")
    scene_load.add_argument("name")
    scene_load.add_argument("--keep-gains", action="store_true",
                             help="Regrava os ganhos de pré anteriores após carregar")
    scene_save = scene_sub.add_parser("save", help="Salva uma cena")
    scene_save.add_argument("name")

    preamp = sub.add_parser("preamp", help="Atalho para um canal de entrada analógico (1-8)")
    preamp.add_argument("channel")
    preamp_sub = preamp.add_subparsers(dest="preamp_cmd", required=True)
    preamp_gain = preamp_sub.add_parser("gain", help="Ganho do pré, em dB")
    preamp_gain.add_argument("db")
    for name in ("phantom", "pad", "hpf"):
        sp = preamp_sub.add_parser(name)
        sp.add_argument("state", choices=["on", "off"])

    meters = sub.add_parser("meters", help="Medidores de nível (valores crus, sem calibração)")
    meters.add_argument("--once", action="store_true",
                         help="Imprime um snapshot JSON e sai, em vez de atualizar continuamente")

    route = sub.add_parser("route", help="Fonte de phones1/phones2/spdif")
    route.add_argument("target", choices=sorted(_ROUTE_TARGETS))
    route.add_argument("source", help="Rótulo (ex.: 'Loopback 1') ou índice 0-based")

    return p


def _parse_set_value(raw: str):
    if raw.lower() == "on":
        return 1
    if raw.lower() == "off":
        return 0
    return float(raw)


def _format_set_result(c: Client, path: str, echoed: object) -> str:
    """"21 (0.202)" for a linear param (human units, then the raw echo);
    the raw echo alone otherwise (controller decision, fix round 1)."""
    human_value = c.to_human(path, echoed)
    if human_value is None:
        return str(echoed)
    try:
        is_int = c.param_row(path).get("type") == "int"
    except KeyError:
        is_int = False
    human_str = str(round(human_value)) if is_int else f"{human_value:.1f}"
    return f"{human_str} ({echoed:.3f})"


def _labeled_source(c: Client, path: str):
    """Value for a *_src/spdifSource path: the matching label when the PL
    label list for it is known (index = round(value * (n - 1))), else the
    raw normalized value."""
    value = c.get(path)
    labels = c.lists.get(path)
    if labels:
        idx = round(value * (len(labels) - 1))
        if 0 <= idx < len(labels):
            return labels[idx]
    return value


def _handle_broken_pipe() -> None:
    """Downstream reader gone (e.g. `quantum-hd8 meters | head -5`):
    redirect stdout to devnull -- the recipe from the Python docs
    (https://docs.python.org/3/library/signal.html#note-on-sigpipe) --
    so the interpreter doesn't print an "Exception ignored" warning for
    the pending flush at exit. Best-effort: sys.stdout may not have a
    real fd (e.g. under a test double), in which case there is nothing to
    redirect and we just swallow it."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass


def _meters_snapshot(m: dict, labels: dict) -> dict:
    """{"in": {label: value, ...}, "aux": {...}, "main": {...}} -- the CLI
    `meters --once` JSON shape (task-9 ruling 5). m may be the
    ucnet.parse_meters() fallback shape {"raw": [...]} when the footer
    didn't match the known layout; that is passed through as-is."""
    if "raw" in m:
        return {"raw": m["raw"]}
    return {
        section: dict(zip(labels[section], m[section]))
        for section in METER_SECTIONS
    }


def _meters_lines(m: dict, labels: dict) -> list[str]:
    if "raw" in m:
        return [f"raw[{i}]: {v}" for i, v in enumerate(m["raw"])]
    lines = []
    for section in METER_SECTIONS:
        for label, value in zip(labels[section], m[section]):
            lines.append(f"{label}: {value}")
    return lines


def _summary_lines(c: Client) -> list[str]:
    lines = []
    for ch in range(1, 9):
        base = f"line/ch{ch}"
        gain = c.human(f"{base}/preampgain")
        v48 = "on" if c.get(f"{base}/48v") else "off"
        pad = "on" if c.get(f"{base}/pad") else "off"
        lines.append(f"{base}: preamp {gain}, 48V {v48}, pad {pad}")
    lines.append(
        f"main/ch1: mute {'on' if c.get('main/ch1/mute') else 'off'}, "
        f"volume {c.human('main/ch1/volume')}")
    for path in ("global/phones1_src", "global/phones2_src", "global/spdifSource"):
        lines.append(f"{path} = {_labeled_source(c, path)}")
    lines.append("cenas: " + ", ".join(c.scenes))
    return lines


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    if not args.cmd:
        p.print_help()
        return 0

    raw_file = None
    raw_sink = None
    if args.cmd == "listen" and args.raw:
        raw_file = open(args.raw, "ab")
        raw_sink = raw_file.write

    udp_factory = default_udp_factory if args.cmd == "meters" else None
    c = Client(raw_sink=raw_sink, udp_factory=udp_factory)
    try:
        c.connect()

        if args.cmd == "dump":
            state = dict(sorted(c.state.items()))
            text = json.dumps(state, indent=2, ensure_ascii=False)
            if args.out:
                with open(args.out, "w") as f:
                    f.write(text)
            else:
                print(text)
            return 0

        if args.cmd == "get":
            try:
                value = c.get(args.path)
            except KeyError:
                matches = difflib.get_close_matches(args.path, c.state.keys(), n=5)
                print(f"caminho desconhecido: {args.path}", file=sys.stderr)
                if matches:
                    print("você quis dizer: " + ", ".join(matches), file=sys.stderr)
                return 1
            print(value)
            return 0

        if args.cmd == "state":
            print("\n".join(_summary_lines(c)))
            return 0

        if args.cmd == "listen":
            try:
                for path, value in c.events(timeout=None):
                    print(f"{path} = {value}")
            except KeyboardInterrupt:
                pass
            return 0

        if args.cmd == "set":
            try:
                value = _parse_set_value(args.value)
            except ValueError:
                print(f"valor inválido: {args.value}", file=sys.stderr)
                return 1
            try:
                echoed = c.set(args.path, value)
            except (KeyError, ValueError, PermissionError, WriteNotConfirmed) as e:
                print(str(e), file=sys.stderr)
                return 1
            print(f"{args.path} = {_format_set_result(c, args.path, echoed)}")
            return 0

        if args.cmd == "undo":
            entry = undo.pop(journal=c.undo_journal)
            if entry is None:
                print("nada para desfazer")
                return 0
            path, before = entry
            try:
                echoed = c.set_raw(path, before)
            except WriteNotConfirmed as e:
                print(str(e), file=sys.stderr)
                return 1
            print(f"{path}: restaurado para {echoed}")
            return 0

        if args.cmd == "scene":
            if args.scene_cmd == "list":
                print("\n".join(c.scenes))
                return 0
            if args.scene_cmd == "load":
                try:
                    result = c.load_scene(args.name, keep_gains=args.keep_gains)
                except SceneLoadTimeout as e:
                    print(f"cena não confirmada (RecalledPreset não chegou): {e}", file=sys.stderr)
                    return 1
                for path, before, after in result["gains"]:
                    print(f"{path}: {before:.1f} dB -> {after:.1f} dB (restaurado)")
                return 0
            if args.scene_cmd == "save":
                print("scene save: formato ainda não capturado", file=sys.stderr)
                return 2
            return 0

        if args.cmd == "preamp":
            try:
                ch = int(args.channel)
            except ValueError:
                ch = None
            if ch is None or not (1 <= ch <= 8):
                print(f"canal inválido: {args.channel!r} (use 1-8)", file=sys.stderr)
                return 2

            base = f"line/ch{ch}"
            if args.preamp_cmd == "gain":
                try:
                    value = float(args.db)
                except ValueError:
                    print(f"valor inválido: {args.db}", file=sys.stderr)
                    return 2
                path = f"{base}/preampgain"
            else:
                value = 1 if args.state == "on" else 0
                path = f"{base}/{_PREAMP_TOGGLE_PARAMS[args.preamp_cmd]}"

            try:
                echoed = c.set(path, value)
            except (KeyError, ValueError, PermissionError, WriteNotConfirmed) as e:
                print(str(e), file=sys.stderr)
                return 1
            print(f"{path} = {_format_set_result(c, path, echoed)}")
            return 0

        if args.cmd == "meters":
            labels = c.meter_labels()
            if args.once:
                try:
                    m = c.read_meters()
                    print(json.dumps(_meters_snapshot(m, labels), ensure_ascii=False))
                except BrokenPipeError:
                    _handle_broken_pipe()
                return 0

            is_tty = sys.stdout.isatty()
            consecutive_timeouts = 0
            try:
                if not is_tty:
                    # Scrolling terminal / redirected output: header once,
                    # then one block of lines per packet (rate = packets
                    # arriving, ~4-5 Hz measured -- no artificial sleep).
                    print(METER_HEADER)
                while True:
                    try:
                        m = c.read_meters()
                    except socket.timeout:
                        consecutive_timeouts += 1
                        if consecutive_timeouts >= METER_MAX_CONSECUTIVE_TIMEOUTS:
                            print("sem medidores do daemon há 5 s", file=sys.stderr)
                            return 1
                        continue
                    except OSError as e:
                        print(f"erro lendo medidores: {e}", file=sys.stderr)
                        return 1
                    consecutive_timeouts = 0
                    lines = "\n".join(_meters_lines(m, labels))
                    if is_tty:
                        # In-place redraw: clear + cursor home + header +
                        # lines, repainted on every packet so scrollback
                        # never fills with 66-line blocks.
                        print(f"{METER_CLEAR_SCREEN}{METER_HEADER}\n{lines}")
                    else:
                        print(lines)
            except KeyboardInterrupt:
                pass
            except BrokenPipeError:
                # Downstream reader closed (e.g. `quantum-hd8 meters | head -5`).
                _handle_broken_pipe()
            return 0

        if args.cmd == "route":
            path = _ROUTE_TARGETS[args.target]
            labels = c.lists.get(path) or STATIC_LABELS[path]
            idx = _resolve_label_index(args.source, labels)
            if idx is None:
                print(f"fonte desconhecida: {args.source!r}", file=sys.stderr)
                print("válidas: " + ", ".join(labels), file=sys.stderr)
                return 2

            try:
                echoed = c.set_list(path, idx, len(labels))
            except WriteNotConfirmed as e:
                print(str(e), file=sys.stderr)
                return 1
            result_idx = round(echoed * (len(labels) - 1))
            result_label = labels[result_idx] if 0 <= result_idx < len(labels) else echoed
            print(f"{args.target} = {result_label}")
            return 0

        return 0
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        c.close()
        if raw_file is not None:
            raw_file.close()


if __name__ == "__main__":
    sys.exit(main())
