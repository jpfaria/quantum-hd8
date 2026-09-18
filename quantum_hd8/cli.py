import argparse
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quantum-hd8")
    p.add_argument("--version", action="version", version=f"quantum-hd8 {__version__}")
    p.add_subparsers(dest="cmd")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if not args.cmd:
        p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
