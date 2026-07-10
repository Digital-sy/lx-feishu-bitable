#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Refresh product performance aggregate from ODS MySQL to two Feishu Bitable tables:
- 90-day store-country-SPU aggregate table
- 7-day store-country-SPU aggregate table

The script intentionally does NOT write raw ods_lx_product_performance detail rows to Feishu.
Raw detail volume is too large for Bitable. It writes business-friendly aggregate rows instead.
"""
import argparse
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Tuple

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common import FeishuBitableClient, get_logger, quote_identifier, settings
from common.database import db_cursor

logger = get_logger("sync_product_performance")

DEFAULT_HARD_SYNC_ROW_LIMIT = 200000

AGG_FIELD_SPECS: List[Dict[str, Any]] = [
    {"name": "父asin", "type": 1, "precision": 0},
    {"name": "统计窗口", "type": 1, "precision": 0},
    {"name": "开始日期", "type": 5, "precision": 0},
    {"name": "结束日期", "type": 5, "precision": 0},
    {"name": "店铺", "type": 1, "precision": 0},
    {"name": "国家", "type": 1, "precision": 0},
    {"name": "SPU", "type": 1, "precision": 0},
    {"name": "销量", "type": 2, "precision": 0},
    {"name": "订单量", "type": 2, "precision": 0},
    {"name": "销售额", "type": 2, "precision": 2},
    {"name": "净销售额", "type": 2, "precision": 2},
    {"name": "毛利润", "type": 2, "precision": 2},
    {"name": "广告花费", "type": 2, "precision": 2},
    {"name": "广告销售额", "type": 2, "precision": 2},
    {"name": "点击量", "type": 2, "precision": 0},
    {"name": "展示量", "type": 2, "precision": 0},
    {"name": "Sessions", "type": 2, "precision": 0},
    {"name": "转化率", "type": 2, "precision": 6},
    {"name": "ACOS", "type": 2, "precision": 6},
    {"name": "TACOS", "type": 2, "precision": 6},
    {"name": "ROAS", "type": 2, "precision": 6},
    {"name": "退货量", "type": 2, "precision": 0},
    {"name": "退货率", "type": 2, "precision": 6},
    {"name": "当前FBA可售库存", "type": 2, "precision": 0},
]

REQUIRED_SOURCE_COLUMNS = {
    "parent_asin",
    "seller_name",
    "country",
    "spu",
    "volume",
    "order_items",
    "amount",
    "net_amount",
    "gross_profit",
    "spend",
    "ad_sales_amount",
    "clicks",
    "impressions",
    "sessions_total",
    "return_count",
    "afn_fulfillable_quantity",
}


def get_source_columns(source_table: str) -> List[Dict[str, Any]]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COLUMN_NAME,
                DATA_TYPE,
                NUMERIC_SCALE,
                ORDINAL_POSITION
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (source_table,),
        )
        columns = cursor.fetchall()
    if not columns:
        raise RuntimeError(f"未找到源表字段: {source_table}，请检查 DB_DATABASE/SOURCE_TABLE 配置")
    logger.info(f"源表 {source_table} 字段数: {len(columns)}")
    return columns


def validate_source_columns(columns: List[Dict[str, Any]], date_column: str) -> None:
    column_names = {col["COLUMN_NAME"] for col in columns}
    missing = sorted((REQUIRED_SOURCE_COLUMNS | {date_column}) - column_names)
    if missing:
        raise RuntimeError(f"源表缺少聚合所需字段: {', '.join(missing)}")


def get_window_bounds(source_table: str, date_column: str, days: int, window_mode: str) -> Tuple[date, date, date]:
    table_sql = quote_identifier(source_table)
    date_sql = quote_identifier(date_column)

    if window_mode == "current_date":
        end_date = date.today()
    else:
        logger.info(f"开始获取源表最大日期: SELECT MAX({date_column}) FROM {source_table}")
        with db_cursor() as cursor:
            cursor.execute(f"SELECT MAX({date_sql}) AS max_dt FROM {table_sql} WHERE {date_sql} IS NOT NULL")
            row = cursor.fetchone()
        max_dt = row.get("max_dt") if row else None
        if max_dt is None:
            raise RuntimeError(f"源表 {source_table}.{date_column} 没有可用日期")
        if isinstance(max_dt, datetime):
            end_date = max_dt.date()
        elif isinstance(max_dt, date):
            end_date = max_dt
        else:
            end_date = datetime.strptime(str(max_dt)[:10], "%Y-%m-%d").date()
        logger.info(f"源表最大日期: {end_date}")

    start_date = end_date - timedelta(days=days - 1)
    end_exclusive = end_date + timedelta(days=1)
    return start_date, end_date, end_exclusive


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            clean[key] = float(value)
        elif isinstance(value, bytes):
            clean[key] = value.decode("utf-8", errors="replace")
        else:
            clean[key] = value
    return clean


def get_product_performance_window_only(source_table: str, date_column: str, days: int, window_mode: str) -> Tuple[date, date]:
    start_date, end_date, _ = get_window_bounds(source_table, date_column, days, window_mode)
    logger.info(f"{days} 天窗口: {start_date} ~ {end_date}（店铺-国家-SPU聚合，未统计行数）")
    return start_date, end_date


def count_product_performance_aggregate(source_table: str, date_column: str, days: int, window_mode: str) -> Tuple[int, date, date]:
    start_date, end_date, end_exclusive = get_window_bounds(source_table, date_column, days, window_mode)
    table_sql = quote_identifier(source_table)
    date_sql = quote_identifier(date_column)
    sql = f"""
        SELECT COUNT(*) AS cnt
        FROM (
            SELECT
                seller_name,
                country,
                spu
            FROM {table_sql}
            WHERE {date_sql} >= %s
              AND {date_sql} < %s
              AND seller_name IS NOT NULL
              AND seller_name <> ''
              AND country IS NOT NULL
              AND country <> ''
              AND spu IS NOT NULL
              AND spu <> ''
            GROUP BY seller_name, country, spu
            HAVING SUM(COALESCE(volume, 0)) <> 0
        ) agg
    """
    logger.info(f"开始统计 {days} 天店铺-国家-SPU聚合行数: {start_date} ~ {end_date}")
    with db_cursor() as cursor:
        cursor.execute(sql, (start_date, end_exclusive))
        row = cursor.fetchone()
    count = int((row or {}).get("cnt") or 0)
    logger.info(f"统计 {days} 天聚合行数完成: {start_date} ~ {end_date}，共 {count} 行")
    return count, start_date, end_date


def read_product_performance_aggregate(
    source_table: str,
    date_column: str,
    days: int,
    window_mode: str,
) -> Tuple[List[Dict[str, Any]], date, date]:
    start_date, end_date, end_exclusive = get_window_bounds(source_table, date_column, days, window_mode)
    table_sql = quote_identifier(source_table)
    date_sql = quote_identifier(date_column)
    window_name = f"{days}天"

    sql = f"""
        SELECT
            COALESCE(MAX(NULLIF(parent_asin, '')), '') AS `父asin`,
            %s AS `统计窗口`,
            %s AS `开始日期`,
            %s AS `结束日期`,
            seller_name AS `店铺`,
            country AS `国家`,
            spu AS `SPU`,
            SUM(COALESCE(volume, 0)) AS `销量`,
            SUM(COALESCE(order_items, 0)) AS `订单量`,
            ROUND(SUM(COALESCE(amount, 0)), 2) AS `销售额`,
            ROUND(SUM(COALESCE(net_amount, 0)), 2) AS `净销售额`,
            ROUND(SUM(COALESCE(gross_profit, 0)), 2) AS `毛利润`,
            ROUND(SUM(COALESCE(spend, 0)), 2) AS `广告花费`,
            ROUND(SUM(COALESCE(ad_sales_amount, 0)), 2) AS `广告销售额`,
            SUM(COALESCE(clicks, 0)) AS `点击量`,
            SUM(COALESCE(impressions, 0)) AS `展示量`,
            SUM(COALESCE(sessions_total, 0)) AS `Sessions`,
            ROUND(SUM(COALESCE(volume, 0)) / NULLIF(SUM(COALESCE(sessions_total, 0)), 0), 6) AS `转化率`,
            ROUND(SUM(COALESCE(spend, 0)) / NULLIF(SUM(COALESCE(ad_sales_amount, 0)), 0), 6) AS `ACOS`,
            ROUND(SUM(COALESCE(spend, 0)) / NULLIF(SUM(COALESCE(amount, 0)), 0), 6) AS `TACOS`,
            ROUND(SUM(COALESCE(ad_sales_amount, 0)) / NULLIF(SUM(COALESCE(spend, 0)), 0), 6) AS `ROAS`,
            SUM(COALESCE(return_count, 0)) AS `退货量`,
            ROUND(SUM(COALESCE(return_count, 0)) / NULLIF(SUM(COALESCE(volume, 0)), 0), 6) AS `退货率`,
            SUM(
                CASE
                    WHEN {date_sql} >= %s AND {date_sql} < %s
                    THEN COALESCE(afn_fulfillable_quantity, 0)
                    ELSE 0
                END
            ) AS `当前FBA可售库存`
        FROM {table_sql}
        WHERE {date_sql} >= %s
          AND {date_sql} < %s
          AND seller_name IS NOT NULL
          AND seller_name <> ''
          AND country IS NOT NULL
          AND country <> ''
          AND spu IS NOT NULL
          AND spu <> ''
        GROUP BY seller_name, country, spu
        HAVING SUM(COALESCE(volume, 0)) <> 0
        ORDER BY `销量` DESC, `店铺`, `国家`, `SPU`
    """

    logger.info(f"开始读取 {days} 天店铺-国家-SPU聚合数据: {start_date} ~ {end_date}")
    params = (window_name, start_date, end_date, end_date, end_exclusive, start_date, end_exclusive)
    with db_cursor() as cursor:
        cursor.execute(sql, params)
        rows = [normalize_row(row) for row in cursor.fetchall()]

    logger.info(f"读取 {days} 天聚合数据完成: {start_date} ~ {end_date}，共 {len(rows)} 行")
    return rows, start_date, end_date


async def resolve_target_tables(client: FeishuBitableClient) -> Tuple[str, str]:
    logger.info("开始解析飞书目标表...")
    table_90d_id = await client.resolve_table_id(settings.FEISHU_90D_TABLE_ID, settings.FEISHU_90D_TABLE_NAME)
    table_7d_id = await client.resolve_table_id(settings.FEISHU_7D_TABLE_ID, settings.FEISHU_7D_TABLE_NAME)
    logger.info("飞书目标表解析完成")
    return table_90d_id, table_7d_id


async def refresh_one_window(
    client: FeishuBitableClient,
    table_id: str,
    table_name: str,
    field_specs: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    days: int,
    start_date: date,
    end_date: date,
    force_large_sync: bool,
) -> None:
    logger.info("=" * 80)
    logger.info(f"准备刷新飞书表: {table_name or table_id} / {days}天 / {start_date} ~ {end_date}")
    logger.info("=" * 80)

    max_records = settings.MAX_FEISHU_RECORDS if settings.MAX_FEISHU_RECORDS > 0 else DEFAULT_HARD_SYNC_ROW_LIMIT
    if len(rows) > max_records and not force_large_sync:
        raise RuntimeError(
            f"{days}天聚合数据共 {len(rows)} 行，超过安全上限 {max_records}。"
            "请进一步聚合、筛选Top N，或使用 --force-large-sync 强制执行。"
        )

    await client.ensure_fields(table_id, field_specs)
    deleted = await client.delete_all_records(table_id)
    logger.info(f"已清空旧数据: {deleted} 行")
    written = await client.batch_create_records(table_id, rows, batch_size=500)
    logger.info(f"✅ 飞书表刷新完成: {table_name or table_id}，写入 {written} 行")


async def async_main(args: argparse.Namespace) -> None:
    if not settings.validate():
        raise SystemExit(1)

    source_table = args.source_table or settings.SOURCE_TABLE
    date_column = args.date_column or settings.DATE_COLUMN
    window_mode = args.window_mode or settings.WINDOW_MODE

    columns = get_source_columns(source_table)
    validate_source_columns(columns, date_column)

    if args.dry_run:
        if args.count_rows:
            count_90d, start_90d, end_90d = count_product_performance_aggregate(source_table, date_column, 90, window_mode)
            count_7d, start_7d, end_7d = count_product_performance_aggregate(source_table, date_column, 7, window_mode)
            count_msg_90d = f"{count_90d} 行"
            count_msg_7d = f"{count_7d} 行"
        else:
            start_90d, end_90d = get_product_performance_window_only(source_table, date_column, 90, window_mode)
            start_7d, end_7d = get_product_performance_window_only(source_table, date_column, 7, window_mode)
            count_msg_90d = "未统计行数"
            count_msg_7d = "未统计行数"

        logger.info("=" * 80)
        logger.info("dry-run 数据库检查完成：不会清空或写入飞书")
        logger.info(f"90天店铺-国家-SPU聚合窗口: {start_90d} ~ {end_90d} / {count_msg_90d}")
        logger.info(f"7天店铺-国家-SPU聚合窗口: {start_7d} ~ {end_7d} / {count_msg_7d}")
        logger.info("如需统计聚合行数，请追加参数: --count-rows。")
        logger.info("如需同时检查飞书 token/table，请追加参数: --check-feishu")
        logger.info("=" * 80)
        if not args.check_feishu:
            return

    client = FeishuBitableClient(settings.FEISHU_APP_TOKEN)
    table_90d_id, table_7d_id = await resolve_target_tables(client)

    if args.dry_run:
        logger.info("=" * 80)
        logger.info("dry-run 飞书检查完成：不会清空或写入飞书")
        logger.info(f"90天表: {settings.FEISHU_90D_TABLE_NAME or table_90d_id} => {table_90d_id}")
        logger.info(f"7天表: {settings.FEISHU_7D_TABLE_NAME or table_7d_id} => {table_7d_id}")
        logger.info("=" * 80)
        return

    rows_90d, start_90d, end_90d = read_product_performance_aggregate(source_table, date_column, 90, window_mode)
    rows_7d, start_7d, end_7d = read_product_performance_aggregate(source_table, date_column, 7, window_mode)

    logger.info("=" * 80)
    logger.info("正式同步前聚合行数预检")
    logger.info(f"90天表将写入: {len(rows_90d)} 行")
    logger.info(f"7天表将写入: {len(rows_7d)} 行")
    logger.info("=" * 80)

    await refresh_one_window(
        client=client,
        table_id=table_90d_id,
        table_name=settings.FEISHU_90D_TABLE_NAME,
        field_specs=AGG_FIELD_SPECS,
        rows=rows_90d,
        days=90,
        start_date=start_90d,
        end_date=end_90d,
        force_large_sync=args.force_large_sync,
    )
    await refresh_one_window(
        client=client,
        table_id=table_7d_id,
        table_name=settings.FEISHU_7D_TABLE_NAME,
        field_specs=AGG_FIELD_SPECS,
        rows=rows_7d,
        days=7,
        start_date=start_7d,
        end_date=end_7d,
        force_large_sync=args.force_large_sync,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步 ods_lx_product_performance 店铺-国家-SPU聚合数据到飞书多维表")
    parser.add_argument("--source-table", default="", help="源表名，默认读取 SOURCE_TABLE")
    parser.add_argument("--date-column", default="", help="日期字段，默认读取 DATE_COLUMN")
    parser.add_argument(
        "--window-mode",
        choices=["latest_date", "current_date"],
        default="",
        help="窗口结束日期：latest_date=源表MAX日期；current_date=服务器当天",
    )
    parser.add_argument("--dry-run", action="store_true", help="只检查数据库窗口日期；默认不访问飞书、不统计行数、不清空、不写入")
    parser.add_argument("--count-rows", action="store_true", help="配合 --dry-run 使用：额外统计店铺-国家-SPU聚合行数")
    parser.add_argument("--check-feishu", action="store_true", help="配合 --dry-run 使用：额外检查飞书 token 和目标 table 解析")
    parser.add_argument("--force-large-sync", action="store_true", help="强制同步超过安全上限的聚合数据")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
