from __future__ import annotations

import argparse
from importlib import import_module
import sys


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description='OA capabilities: approvals, local materials and reference data.')
    parser.add_argument('capability', choices=('approvals', 'materials', 'references'))
    if not arguments or arguments[0] in {'-h', '--help'}:
        parser.parse_args(arguments)
    capability = parser.parse_args(arguments[:1]).capability
    return import_module(f'apps.oa.{capability}.cli').main(arguments[1:])


if __name__ == '__main__':
    raise SystemExit(main())
