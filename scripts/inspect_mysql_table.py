#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探查 MySQL 表字段、注释和样例数据。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import quote_identifier  # noqa: E402
from common.database import db_cursor  # noqa: E402


KEYWORDS = (
    "session", "visit", "traffic", "page", "view",
    "order", "click", "impression", "sku", "spu",
)


def get_columns(schema: str, table: str) -> List[Dict[str, Any]]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                ORDINAL_POSITION,
                COLUMN_NAME,
                COLUMN_TYPE,
                IS_NULLABLE,
                COLUMN_DEFAULT,
                COLUMN_KEY,
                EXTRA,
                COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (schema, table),
        )
        return list(cursor.fetchall())


def get_sample_rows(schema: str, table: str, limit: int) -> List[Dict[str, Any]]:
    sql = (
        f"SELECT * FROM {quote_identifier(schema)}.{quote_identifier(table)} "
        f"LIMIT {int(limit)}"
    )
    with db_cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def print_columns(columns: List[Dict[str, Any]]) -> None:
    print("=" * 120)
    print("字段结构")
    print("=" * 120)
    print(f"{'序号':<6}{'字段名':<34}{'类型':<24}{'可空':<8}{'键':<8}{'字段注释'}")
    for column in columns:
        print(
            f"{column['ORDINAL_POSITION']:<6}"
            f"{str(column['COLUMN_NAME']):<34}"
            f"{str(column['COLUMN_TYPE']):<24}"
            f"{str(column['IS_NULLABLE']):<8}"
            f"{str(column['COLUMN_KEY'] or ''):<8}"
            f"{str(column['COLUMN_COMMENT'] or '')}"
        )


def print_candidates(columns: List[Dict[str, Any]]) -> None:
    matches = []
    for column in columns:
        name = str(column["COLUMN_NAME"] or "")
        comment = str(column["COLUMN_COMMENT"] or "")
        haystack = f"{name} {comment}".lower()
        if any(keyword in haystack for keyword in KEYWORDS):
            matches.append(column)

    print("\n" + "=" * 120)
    print("可能相关的候选字段")
    print("=" * 120)
    if not matches:
        print("未识别到候选字段。")
        return
    for column in matches:
        print(
            f"- {column['COLUMN_NAME']} | {column['COLUMN_TYPE']} | "
            f"{column['COLUMN_COMMENT'] or '无注释'}"
        )


def print_samples(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 120)
    print(f"样例数据（{len(rows)} 条）")
    print("=" * 120)
    for index, row in enumerate(rows, start=1):
        print(f"\n[{index}]")
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="只读探查 MySQL 表字段和样例数据")
    parser.add_argument("--schema", required=True, help="数据库名，例如 dws_db")
    parser.add_argument("--table", required=True, help="表名")
    parser.add_argument("--sample-size", type=int, default=3, help="样例行数，默认3，最大20")
    args = parser.parse_args()

    if args.sample_size < 0 or args.sample_size > 20:
        raise SystemExit("--sample-size 必须在 0 到 20 之间")

    columns = get_columns(args.schema, args.table)
    if not columns:
        raise SystemExit(f"未找到表: {args.schema}.{args.table}")

    print(f"目标表: {args.schema}.{args.table}")
    print(f"字段数: {len(columns)}")
    print_columns(columns)
    print_candidates(columns)

    if args.sample_size:
        rows = get_sample_rows(args.schema, args.table, args.sample_size)
        print_samples(rows)

    print("\n探查完成：未修改数据库。")


if __name__ == "__main__":
    main()
