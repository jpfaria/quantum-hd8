import argparse
import difflib
import json
import sys

from . import __version__
from .client import Client


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quantum-hd8")
    p.add_argument("--version", action="version", version=f"quantum-hd8 {__version__}")
    sub = p.add_subparsers(dest="cmd")

    dump = sub.add_parser("dump", help="Estado achatado em JSON")
    dump.add_argument("--out", metavar="ARQUIVO")

    get = sub.add_parser("get", help="Valor de um caminho")
    get.add_argument("path")

    sub.add_parser("state", help="Resumo legível do estado")
    sub.add_parser("listen", help="Imprime eventos até Ctrl-C")

    return p


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
        lines.append(f"{path} = {c.get(path)}")
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

    c = Client()
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

        return 0
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
