#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Refresh ods_lx_product_performance from ODS MySQL to two Feishu Bitable tables:
- 90-day table
- 7-day table

Default window end uses MAX(dt) in source table, not server current date. This is safer when
ODS data has delayed ingestion.
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

MYSQL_NUMBER_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
    "decimal", "numeric", "float", "double", "real",
}
MYSQL_DATE_TYPES = {"date", "datetime", "timestamp"}
MYSQL_TEXT_TYPES = {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "json", "enum", "set"}


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


def build_field_specs(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs = []
    for col in columns:
        name = col["COLUMN_NAME"]
        data_type = str(col["DATA_TYPE"] or "").lower()
        if data_type in MYSQL_NUMBER_TYPES:
            precision = int(col.get("NUMERIC_SCALE") or 0)
            specs.append({"name": name, "type": 2, "precision": min(max(precision, 0), 10)})
        elif data_type in MYSQL_DATE_TYPES:
            specs.append({"name": name, "type": 5, "precision": 0})
        elif data_type in MYSQL_TEXT_TYPES:
            specs.append({"name": name, "type": 1, "precision": 0})
        else:
            logger.warning(f"字段 {name} 的 MySQL 类型 {data_type} 未显式映射，按文本写入飞书")
            specs.append({"name": name, "type": 1, "precision": 0})
    return specs


def get_window_bounds(source_table: str, date_column: str, days: int, window_mode: str) -> Tuple[date, date, date]:
    table_sql = quote_identifier(source_table)
    date_sql = quote_identifier(date_column)

    if window_mode == "current_date":
        end_date = date.today()
    else:
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


def read_product_performance_window(source_table: str, date_column: str, days: int, window_mode: str) -> Tuple[List[Dict[str, Any]], date, date]:
    start_date, end_date, end_exclusive = get_window_bounds(source_table, date_column, days, window_mode)
    table_sql = quote_identifier(source_table)
    date_sql = quote_identifier(date_column)

    order_columns = [date_column, "store_name", "country", "msku", "asin"]
    order_sql_parts = []
    source_columns = {col["COLUMN_NAME"] for col in get_source_columns(source_table)}
    for col in order_columns:
        if col in source_columns:
            direction = "DESC" if col == date_column else "ASC"
            order_sql_parts.append(f"{quote_identifier(col)} {direction}")
    order_sql = "ORDER BY " + ", ".join(order_sql_parts) if order_sql_parts else ""

    sql = f"""
        SELECT *
        FROM {table_sql}
        WHERE {date_sql} >= %s
          AND {date_sql} < %s
        {order_sql}
    """

    with db_cursor() as cursor:
        cursor.execute(sql, (start_date, end_exclusive))
        rows = [normalize_row(row) for row in cursor.fetchall()]

    logger.info(f"读取 {days} 天窗口数据: {start_date} ~ {end_date}，共 {len(rows)} 行")
    return rows, start_date, end_date


async def refresh_one_window(
    client: FeishuBitableClient,
    table_id: str,
    table_name: str,
    field_specs: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    days: int,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> None:
    logger.info("=" * 80)
    logger.info(f"准备刷新飞书表: {table_name or table_id} / {days}天 / {start_date} ~ {end_date}")
    logger.info("=" * 80)

    max_records = settings.MAX_FEISHU_RECORDS
    if max_records > 0 and len(rows) > max_records:
        raise RuntimeError(
            f"{days}天数据共 {len(rows)} 行，超过 MAX_FEISHU_RECORDS={max_records}。"
            "请提高飞书容量、缩小窗口，或把 MAX_FEISHU_RECORDS 改为 0 后由飞书 API 自身判断。"
        )

    if dry_run:
        logger.info(f"dry-run：不会清空或写入飞书。目标行数: {len(rows)}")
        return

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
    field_specs = build_field_specs(columns)

    client = FeishuBitableClient(settings.FEISHU_APP_TOKEN)
    table_90d_id = await client.resolve_table_id(settings.FEISHU_90D_TABLE_ID, settings.FEISHU_90D_TABLE_NAME)
    table_7d_id = await client.resolve_table_id(settings.FEISHU_7D_TABLE_ID, settings.FEISHU_7D_TABLE_NAME)

    rows_90d, start_90d, end_90d = read_product_performance_window(source_table, date_column, 90, window_mode)
    rows_7d, start_7d, end_7d = read_product_performance_window(source_table, date_column, 7, window_mode)

    await refresh_one_window(
        client=client,
        table_id=table_90d_id,
        table_name=settings.FEISHU_90D_TABLE_NAME,
        field_specs=field_specs,
        rows=rows_90d,
        days=90,
        start_date=start_90d,
        end_date=end_90d,
        dry_run=args.dry_run,
    )
    await refresh_one_window(
        client=client,
        table_id=table_7d_id,
        table_name=settings.FEISHU_7D_TABLE_NAME,
        field_specs=field_specs,
        rows=rows_7d,
        days=7,
        start_date=start_7d,
        end_date=end_7d,
        dry_run=args.dry_run,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步 ods_lx_product_performance 到飞书多维表")
    parser.add_argument("--source-table", default="", help="源表名，默认读取 SOURCE_TABLE")
    parser.add_argument("--date-column", default="", help="日期字段，默认读取 DATE_COLUMN")
    parser.add_argument(
        "--window-mode",
        choices=["latest_date", "current_date"],
        default="",
        help="窗口结束日期：latest_date=源表MAX日期；current_date=服务器当天",
    )
    parser.add_argument("--dry-run", action="store_true", help="只读库和解析飞书表，不清空、不写入飞书")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
