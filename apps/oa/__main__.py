from __future__ import annotations

import argparse
import sys


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description='OA capabilities: approvals, local materials and reference data.')
    parser.add_argument('capability', choices=('approvals', 'materials', 'references'))
    if not arguments or arguments[0] in {'-h', '--help'}:
        parser.parse_args(arguments)
    capability = parser.parse_args(arguments[:1]).capability
    if capability == 'approvals':
        from apps.oa.approvals import cli as approvals

        return approvals.main(arguments[1:])
    if capability == 'materials':
        from apps.oa.materials import cli as materials

        return materials.main(arguments[1:])
    from apps.oa.references import cli as references

    return references.main(arguments[1:])


if __name__ == '__main__':
    raise SystemExit(main())
