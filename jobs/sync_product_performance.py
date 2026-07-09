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
DEFAULT_HARD_SYNC_ROW_LIMIT = 200000


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


def build_order_sql(date_column: str, source_column_names: set) -> str:
    order_columns = [date_column, "store_name", "country", "msku", "asin"]
    order_sql_parts = []
    for col in order_columns:
        if col in source_column_names:
            direction = "DESC" if col == date_column else "ASC"
            order_sql_parts.append(f"{quote_identifier(col)} {direction}")
    return "ORDER BY " + ", ".join(order_sql_parts) if order_sql_parts else ""


def count_product_performance_window(source_table: str, date_column: str, days: int, window_mode: str) -> Tuple[int, date, date]:
    """Count rows for a window. This may be slow if date_column has no index."""
    start_date, end_date, end_exclusive = get_window_bounds(source_table, date_column, days, window_mode)
    table_sql = quote_identifier(source_table)
    date_sql = quote_identifier(date_column)
    sql = f"""
        SELECT COUNT(*) AS cnt
        FROM {table_sql}
        WHERE {date_sql} >= %s
          AND {date_sql} < %s
    """
    logger.info(f"开始统计 {days} 天窗口行数: {start_date} ~ {end_date}")
    with db_cursor() as cursor:
        cursor.execute(sql, (start_date, end_exclusive))
        row = cursor.fetchone()
    count = int((row or {}).get("cnt") or 0)
    logger.info(f"统计 {days} 天窗口数据: {start_date} ~ {end_date}，共 {count} 行")
    return count, start_date, end_date


def get_product_performance_window_only(source_table: str, date_column: str, days: int, window_mode: str) -> Tuple[date, date]:
    """Fast dry-run path: only compute window dates, without COUNT(*) or SELECT *."""
    start_date, end_date, _ = get_window_bounds(source_table, date_column, days, window_mode)
    logger.info(f"{days} 天窗口: {start_date} ~ {end_date}（未统计行数）")
    return start_date, end_date


def assert_sync_size_safe(source_table: str, date_column: str, window_mode: str, force_large_sync: bool) -> None:
    """Preflight count before full SELECT * and Feishu refresh."""
    count_90d, start_90d, end_90d = count_product_performance_window(source_table, date_column, 90, window_mode)
    count_7d, start_7d, end_7d = count_product_performance_window(source_table, date_column, 7, window_mode)
    max_records = settings.MAX_FEISHU_RECORDS if settings.MAX_FEISHU_RECORDS > 0 else DEFAULT_HARD_SYNC_ROW_LIMIT

    logger.info("=" * 80)
    logger.info("正式同步前行数预检")
    logger.info(f"90天窗口: {start_90d} ~ {end_90d} / {count_90d} 行")
    logger.info(f"7天窗口: {start_7d} ~ {end_7d} / {count_7d} 行")
    logger.info(f"当前安全上限: {max_records} 行/表")
    logger.info("=" * 80)

    if force_large_sync:
        logger.warning("已启用 --force-large-sync，将跳过行数安全保护。不建议用于飞书明细表。")
        return

    oversized = []
    if count_90d > max_records:
        oversized.append(f"90天表 {count_90d} 行 > {max_records}")
    if count_7d > max_records:
        oversized.append(f"7天表 {count_7d} 行 > {max_records}")
    if oversized:
        raise RuntimeError(
            "正式同步已中止，原因：" + "；".join(oversized) + "。"
            "这个量级不适合直接写飞书明细表。请改为聚合表/异常清单/Top N，"
            "或确认风险后使用 --force-large-sync 强制执行。"
        )


def read_product_performance_window(
    source_table: str,
    date_column: str,
    days: int,
    window_mode: str,
    source_column_names: set,
) -> Tuple[List[Dict[str, Any]], date, date]:
    start_date, end_date, end_exclusive = get_window_bounds(source_table, date_column, days, window_mode)
    table_sql = quote_identifier(source_table)
    date_sql = quote_identifier(date_column)
    order_sql = build_order_sql(date_column, source_column_names)

    sql = f"""
        SELECT *
        FROM {table_sql}
        WHERE {date_sql} >= %s
          AND {date_sql} < %s
        {order_sql}
    """

    logger.info(f"开始读取 {days} 天窗口数据: {start_date} ~ {end_date}")
    with db_cursor() as cursor:
        cursor.execute(sql, (start_date, end_exclusive))
        rows = [normalize_row(row) for row in cursor.fetchall()]

    logger.info(f"读取 {days} 天窗口数据完成: {start_date} ~ {end_date}，共 {len(rows)} 行")
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
) -> None:
    logger.info("=" * 80)
    logger.info(f"准备刷新飞书表: {table_name or table_id} / {days}天 / {start_date} ~ {end_date}")
    logger.info("=" * 80)

    max_records = settings.MAX_FEISHU_RECORDS if settings.MAX_FEISHU_RECORDS > 0 else DEFAULT_HARD_SYNC_ROW_LIMIT
    if len(rows) > max_records:
        raise RuntimeError(
            f"{days}天数据共 {len(rows)} 行，超过安全上限 {max_records}。"
            "请改为聚合表/异常清单/Top N，或使用 --force-large-sync 强制执行。"
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
    source_column_names = {col["COLUMN_NAME"] for col in columns}
    field_specs = build_field_specs(columns)

    if args.dry_run:
        if args.count_rows:
            count_90d, start_90d, end_90d = count_product_performance_window(source_table, date_column, 90, window_mode)
            count_7d, start_7d, end_7d = count_product_performance_window(source_table, date_column, 7, window_mode)
            count_msg_90d = f"{count_90d} 行"
            count_msg_7d = f"{count_7d} 行"
        else:
            start_90d, end_90d = get_product_performance_window_only(source_table, date_column, 90, window_mode)
            start_7d, end_7d = get_product_performance_window_only(source_table, date_column, 7, window_mode)
            count_msg_90d = "未统计行数"
            count_msg_7d = "未统计行数"

        logger.info("=" * 80)
        logger.info("dry-run 数据库检查完成：不会清空或写入飞书")
        logger.info(f"90天窗口: {start_90d} ~ {end_90d} / {count_msg_90d}")
        logger.info(f"7天窗口: {start_7d} ~ {end_7d} / {count_msg_7d}")
        logger.info("如需统计行数，请追加参数: --count-rows。若很慢，说明日期字段可能没有索引。")
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

    assert_sync_size_safe(source_table, date_column, window_mode, args.force_large_sync)

    rows_90d, start_90d, end_90d = read_product_performance_window(
        source_table, date_column, 90, window_mode, source_column_names
    )
    rows_7d, start_7d, end_7d = read_product_performance_window(
        source_table, date_column, 7, window_mode, source_column_names
    )

    await refresh_one_window(
        client=client,
        table_id=table_90d_id,
        table_name=settings.FEISHU_90D_TABLE_NAME,
        field_specs=field_specs,
        rows=rows_90d,
        days=90,
        start_date=start_90d,
        end_date=end_90d,
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
    parser.add_argument("--dry-run", action="store_true", help="只检查数据库窗口日期；默认不访问飞书、不统计行数、不清空、不写入")
    parser.add_argument("--count-rows", action="store_true", help="配合 --dry-run 使用：额外统计90天/7天行数，源表无日期索引时可能很慢")
    parser.add_argument("--check-feishu", action="store_true", help="配合 --dry-run 使用：额外检查飞书 token 和目标 table 解析")
    parser.add_argument("--force-large-sync", action="store_true", help="强制同步超大明细数据。不建议用于飞书，可能内存暴涨或写入失败")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
