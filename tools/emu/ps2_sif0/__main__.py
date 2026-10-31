"""Show usage for the SIF0 model package; it has no standalone input format."""

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import tools.emu.ps2_sif0.launch and call launch() with live bytearrays."
    )
    parser.parse_args()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
