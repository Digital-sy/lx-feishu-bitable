#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读诊断拍摄效果指标源表为什么查询不到数据。"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import quote_identifier  # noqa: E402
from common.database import db_cursor  # noqa: E402


def parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def print_rows(title: str, rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 110)
    print(title)
    print("=" * 110)
    if not rows:
        print("无数据")
        return
    columns = list(rows[0].keys())
    print(" | ".join(columns))
    for row in rows:
        print(" | ".join(str(row.get(col)) for col in columns))


def main() -> None:
    parser = argparse.ArgumentParser(description="诊断拍摄效果回填源数据")
    parser.add_argument("--schema", default="dws_db")
    parser.add_argument("--table", default="dws_op_listing_traffic_daily")
    parser.add_argument("--store", default="JQ-US")
    parser.add_argument("--country", default="US")
    parser.add_argument("--start-date", default="2026-07-01")
    parser.add_argument("--end-date", default="2026-07-20")
    parser.add_argument("--spu", nargs="+", default=["BX504", "BX506", "BX507"])
    args = parser.parse_args()

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    table_sql = f"{quote_identifier(args.schema)}.{quote_identifier(args.table)}"

    with db_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(*) AS total_rows,
                   MIN(dt) AS min_dt,
                   MAX(dt) AS max_dt,
                   SUM(COALESCE(clicks,0)) AS total_clicks,
                   SUM(COALESCE(impressions,0)) AS total_impressions
            FROM {table_sql}
            """
        )
        print_rows("1. 全表数据概况", [cursor.fetchone() or {}])

        cursor.execute(
            f"""
            SELECT store_name, country,
                   COUNT(*) AS rows_count,
                   MIN(dt) AS min_dt,
                   MAX(dt) AS max_dt
            FROM {table_sql}
            WHERE store_name = %s
            GROUP BY store_name, country
            ORDER BY country
            """,
            (args.store,),
        )
        print_rows(f"2. 店铺 {args.store} 实际国家值", cursor.fetchall())

        cursor.execute(
            f"""
            SELECT dt, store_name, country, sku, msku, asin,
                   clicks, impressions, ad_order_qty, ad_cost_amt
            FROM {table_sql}
            WHERE dt BETWEEN %s AND %s
              AND store_name = %s
            ORDER BY dt DESC
            LIMIT 20
            """,
            (start_date, end_date, args.store),
        )
        print_rows(
            f"3. {args.store} 在 {start_date}~{end_date} 的样例数据",
            cursor.fetchall(),
        )

        for spu in args.spu:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS rows_count,
                       MIN(dt) AS min_dt,
                       MAX(dt) AS max_dt,
                       COUNT(DISTINCT store_name) AS store_count,
                       COUNT(DISTINCT country) AS country_count,
                       SUM(COALESCE(clicks,0)) AS clicks,
                       SUM(COALESCE(impressions,0)) AS impressions
                FROM {table_sql}
                WHERE UPPER(TRIM(sku)) LIKE %s
                """,
                (spu.upper() + "%",),
            )
            print_rows(f"4.{spu} 任意日期/任意店铺，SKU前缀匹配", [cursor.fetchone() or {}])

            cursor.execute(
                f"""
                SELECT store_name, country,
                       COUNT(*) AS rows_count,
                       MIN(dt) AS min_dt,
                       MAX(dt) AS max_dt,
                       MIN(sku) AS sample_sku,
                       MAX(sku) AS sample_sku_2
                FROM {table_sql}
                WHERE UPPER(TRIM(sku)) LIKE %s
                GROUP BY store_name, country
                ORDER BY rows_count DESC
                LIMIT 20
                """,
                (spu.upper() + "%",),
            )
            print_rows(f"5.{spu} 实际分布的店铺/国家/SKU", cursor.fetchall())

            cursor.execute(
                f"""
                SELECT COUNT(*) AS rows_count,
                       MIN(dt) AS min_dt,
                       MAX(dt) AS max_dt,
                       SUM(COALESCE(clicks,0)) AS clicks,
                       SUM(COALESCE(impressions,0)) AS impressions,
                       SUM(COALESCE(ad_order_qty,0)) AS ad_order_qty,
                       SUM(COALESCE(ad_cost_amt,0)) AS ad_cost_amt
                FROM {table_sql}
                WHERE dt BETWEEN %s AND %s
                  AND store_name = %s
                  AND country = %s
                  AND UPPER(TRIM(sku)) LIKE %s
                """,
                (start_date, end_date, args.store, args.country, spu.upper() + "%"),
            )
            print_rows(
                f"6.{spu} 精确过滤：{args.store}/{args.country}/{start_date}~{end_date}",
                [cursor.fetchone() or {}],
            )

            cursor.execute(
                f"""
                SELECT dt, store_name, country, sku, msku, asin,
                       clicks, impressions, ad_order_qty, ad_cost_amt
                FROM {table_sql}
                WHERE UPPER(TRIM(sku)) LIKE %s
                ORDER BY dt DESC
                LIMIT 20
                """,
                (spu.upper() + "%",),
            )
            print_rows(f"7.{spu} 最新20条明细", cursor.fetchall())

    print("\n诊断完成：本脚本只读，不修改数据库。")


if __name__ == "__main__":
    main()
