import argparse
import difflib
import json
import sys

from . import __version__
from . import undo
from .client import Client, SceneLoadTimeout, WriteNotConfirmed


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

    c = Client(raw_sink=raw_sink)
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
