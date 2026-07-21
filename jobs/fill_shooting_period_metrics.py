#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import FeishuBitableClient, get_logger, quote_identifier
from common.database import db_cursor

logger = get_logger("fill_shooting_period_metrics")

STORE_MAP = {
    "REORIA": "JQ-US",
    "LASLULU": "MT-US",
    "NIMIN": "RKZ-US",
    "PINKMSTYLE": "SY-US",
}

INPUT_FIELDS = {"款号", "店铺", "更换日期", "更换截至日期"}
OUTPUT_FIELDS = {"CTR", "CPC", "点击量", "转化率", "广告转化率"}
SOURCE_COLUMNS = {
    "dt", "store_name", "country", "spu", "clicks", "impressions",
    "order_qty", "ad_order_qty", "ad_cost_amt", "sessions_total",
}


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(text(item) for item in value).strip()
    if isinstance(value, dict):
        return text(value.get("text") or value.get("name") or value.get("value"))
    return str(value).strip()


def options(value: Any) -> List[str]:
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    return [item for item in (text(value) for value in values) if item]


def parse_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp).date()
    value_text = text(value)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value_text[:19], fmt).date()
        except ValueError:
            pass
    raise ValueError(f"无法解析日期: {value!r}")


async def request(client: FeishuBitableClient, method: str, path: str,
                  params: Optional[Dict[str, Any]] = None,
                  payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers = await client._headers()
    url = f"{client.api_base}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as http:
        response = await http.request(method, url, headers=headers, params=params, json=payload)
    result = response.json()
    if result.get("code") != 0:
        raise RuntimeError(f"飞书接口失败: {result}")
    return result


async def list_records(client: FeishuBitableClient, app_token: str,
                       table_id: str, view_id: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        params: Dict[str, Any] = {"page_size": 500}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        result = await request(
            client, "GET",
            f"bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params=params,
        )
        data = result.get("data") or {}
        rows.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    return rows


async def update_records(client: FeishuBitableClient, app_token: str,
                         table_id: str, updates: List[Dict[str, Any]]) -> None:
    for start in range(0, len(updates), 500):
        batch = updates[start:start + 500]
        await request(
            client, "POST",
            f"bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            payload={"records": batch},
        )
        logger.info(f"已更新 {min(start + len(batch), len(updates))}/{len(updates)}")


def find_table(table_name: str, schema_name: str) -> Tuple[str, str]:
    with db_cursor() as cursor:
        if schema_name:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (schema_name, table_name),
            )
        else:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_NAME=%s ORDER BY (TABLE_SCHEMA=DATABASE()) DESC, "
                "(TABLE_SCHEMA LIKE 'dws%%') DESC, TABLE_SCHEMA",
                (table_name,),
            )
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(f"未找到数据表: {schema_name + '.' if schema_name else ''}{table_name}")
    row = rows[0]
    return str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"])


def validate_table(schema: str, table: str) -> None:
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (schema, table),
        )
        columns = {str(row["COLUMN_NAME"]) for row in cursor.fetchall()}
    missing = sorted(SOURCE_COLUMNS - columns)
    if missing:
        raise RuntimeError(f"源表缺少字段: {', '.join(missing)}")


def query_metrics(schema: str, table: str, spu: str, stores: List[str],
                  start_date: date, end_date: date, country: str) -> Dict[str, Any]:
    placeholders = ",".join(["%s"] * len(stores))
    sql = f"""
        SELECT COUNT(*) source_rows,
               SUM(COALESCE(clicks,0)) clicks,
               SUM(COALESCE(impressions,0)) impressions,
               SUM(COALESCE(order_qty,0)) order_qty,
               SUM(COALESCE(ad_order_qty,0)) ad_order_qty,
               SUM(COALESCE(ad_cost_amt,0)) ad_cost_amt,
               SUM(COALESCE(sessions_total,0)) sessions_total
        FROM {quote_identifier(schema)}.{quote_identifier(table)}
        WHERE dt BETWEEN %s AND %s
          AND spu=%s
          AND store_name IN ({placeholders})
          AND country=%s
    """
    with db_cursor() as cursor:
        cursor.execute(sql, [start_date, end_date, spu, *stores, country])
        row = cursor.fetchone() or {}
    clicks = float(row.get("clicks") or 0)
    impressions = float(row.get("impressions") or 0)
    order_qty = float(row.get("order_qty") or 0)
    ad_order_qty = float(row.get("ad_order_qty") or 0)
    ad_cost_amt = float(row.get("ad_cost_amt") or 0)
    sessions_total = float(row.get("sessions_total") or 0)
    return {
        "source_rows": int(row.get("source_rows") or 0),
        "点击量": int(round(clicks)),
        "CTR": round(clicks / impressions, 6) if impressions else None,
        "CPC": round(ad_cost_amt / clicks, 6) if clicks else None,
        "转化率": round(order_qty / sessions_total, 6) if sessions_total else None,
        "广告转化率": round(ad_order_qty / clicks, 6) if clicks else None,
    }


async def run(args: argparse.Namespace) -> None:
    schema, table = find_table(args.source_table, args.source_schema)
    validate_table(schema, table)
    logger.info(f"数据源: {schema}.{table}")

    client = FeishuBitableClient(args.app_token)
    fields = await client.list_fields(args.table_id)
    if "广告转化率" not in fields:
        await client.create_field(args.table_id, "广告转化率", 2, precision=6)
        fields = await client.list_fields(args.table_id)
    missing = sorted((INPUT_FIELDS | OUTPUT_FIELDS) - set(fields))
    if missing:
        raise RuntimeError(f"飞书表缺少字段: {', '.join(missing)}")
    wrong_types = [name for name in OUTPUT_FIELDS if int(fields[name].get("type") or 0) != 2]
    if wrong_types:
        raise RuntimeError(f"回写字段不是数字类型: {', '.join(sorted(wrong_types))}")

    records = await list_records(client, args.app_token, args.table_id, args.view_id)
    updates: List[Dict[str, Any]] = []
    skipped = 0

    for record in records:
        record_id = str(record.get("record_id") or "")
        values = record.get("fields") or {}
        spu = text(values.get("款号"))
        brands = options(values.get("店铺"))
        try:
            start_date = parse_date(values.get("更换日期"))
            end_date = parse_date(values.get("更换截至日期"))
        except ValueError as exc:
            logger.warning(f"跳过 {spu or record_id}: {exc}")
            skipped += 1
            continue
        unknown = [brand for brand in brands if brand not in STORE_MAP]
        if not record_id or not spu or not brands or not start_date or not end_date or unknown:
            logger.warning(f"跳过 {spu or record_id}: 条件不完整或店铺未映射 {unknown}")
            skipped += 1
            continue
        if end_date < start_date:
            logger.warning(f"跳过 {spu}: 结束日期早于开始日期")
            skipped += 1
            continue
        stores = sorted({STORE_MAP[brand] for brand in brands})
        metrics = query_metrics(schema, table, spu, stores, start_date, end_date, args.country)
        output = {name: metrics[name] for name in OUTPUT_FIELDS}
        updates.append({"record_id": record_id, "fields": output})
        logger.info(
            f"{spu} | {brands}->{stores} | {start_date}~{end_date} | "
            f"rows={metrics['source_rows']} | 点击量={metrics['点击量']} | "
            f"CTR={metrics['CTR']} | CPC={metrics['CPC']} | "
            f"转化率={metrics['转化率']} | 广告转化率={metrics['广告转化率']}"
        )
        if args.limit and len(updates) >= args.limit:
            break

    logger.info(f"生成回写结果 {len(updates)} 条，跳过 {skipped} 条")
    if args.dry_run:
        logger.info("DRY RUN：未修改飞书")
        return
    await update_records(client, args.app_token, args.table_id, updates)
    logger.info("回写完成")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-token", required=True)
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--view-id", default="")
    parser.add_argument("--source-schema", default="")
    parser.add_argument("--source-table", default="dws_op_listing_traffic_daily")
    parser.add_argument("--country", default="US")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
