"""展示原始仓库资料数量，并可构建带数量的批次谱系事实仓。"""
import argparse
import json
from pathlib import Path

from .domain import summarize
from .engine import LedgerError, apply_manifest, build_warehouse
from .io import load
from .report import report_json


def main():
    parser = argparse.ArgumentParser(prog='warehouse_data.cli')
    sub = parser.add_subparsers(dest='command', required=True)

    inspect_parser = sub.add_parser('inspect', help='只读原始资料摘要')
    inspect_parser.add_argument('file')

    build_parser = sub.add_parser(
        'build', help='构建事实仓并输出桥表、路径、损耗与守恒账')
    build_parser.add_argument('file')
    build_parser.add_argument(
        '--apply-to', metavar='BASE_FILE',
        help='在另一份清单构建的仓上增量应用本清单（重放核对）')

    args = parser.parse_args()

    if args.command == 'inspect':
        value, digest = load(args.file)
        print(json.dumps(
            {'manifest_sha256': digest,
             'summary': summarize(value, Path(args.file).parent)},
            ensure_ascii=False, indent=2))
        return

    value, digest = load(args.file)
    try:
        if args.apply_to:
            base_value, _ = load(args.apply_to)
            warehouse = build_warehouse(base_value)
            affected = apply_manifest(warehouse, value)
        else:
            warehouse = build_warehouse(value)
            affected = warehouse.last_affected
    except (LedgerError, ValueError, KeyError) as exc:
        parser.exit(2, f'构建被拒绝: {exc}\n')

    report = json.loads(report_json(warehouse))
    report['manifest_sha256'] = digest
    report['affected_lots'] = affected
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
