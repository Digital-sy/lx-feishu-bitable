#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按飞书记录中的款号、店铺和更换日期区间，回填可由当前 DWS 支持的指标。

当前回填：
- 点击量
- CTR = 点击量 / 展示量
- CPC = 广告花费 / 点击量
- 广告转化率 = 广告订单量 / 点击量

当前 dws_op_listing_traffic_daily 尚无 Sessions 字段，因此本脚本不计算、
不校验、也不更新飞书中的“转化率”字段。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
OUTPUT_FIELDS = {"CTR", "CPC", "点击量", "广告转化率"}
CORE_SOURCE_COLUMNS = {
    "dt",
    "store_name",
    "country",
    "clicks",
    "impressions",
    "ad_order_qty",
    "ad_cost_amt",
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


async def request(
    client: FeishuBitableClient,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    headers = await client._headers()
    url = f"{client.api_base}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as http:
        response = await http.request(
            method,
            url,
            headers=headers,
            params=params,
            json=payload,
        )

    try:
        result = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"飞书接口返回非 JSON，HTTP {response.status_code}: {response.text[:500]}"
        ) from exc

    if result.get("code") != 0:
        raise RuntimeError(f"飞书接口失败: {result}")
    return result


async def list_records(
    client: FeishuBitableClient,
    app_token: str,
    table_id: str,
    view_id: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    page_token = ""

    while True:
        params: Dict[str, Any] = {"page_size": 500}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token

        result = await request(
            client,
            "GET",
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

    logger.info(f"读取飞书记录完成: {len(rows)} 条")
    return rows


async def update_records(
    client: FeishuBitableClient,
    app_token: str,
    table_id: str,
    updates: List[Dict[str, Any]],
) -> None:
    if not updates:
        logger.warning("没有需要回写的记录")
        return

    for start in range(0, len(updates), 500):
        batch = updates[start:start + 500]
        await request(
            client,
            "POST",
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
        target = f"{schema_name}.{table_name}" if schema_name else table_name
        raise RuntimeError(f"未找到数据表: {target}")

    row = rows[0]
    return str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"])


def get_table_columns(schema: str, table: str) -> Set[str]:
    with db_cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (schema, table),
        )
        return {str(row["COLUMN_NAME"]) for row in cursor.fetchall()}


def resolve_source_layout(schema: str, table: str) -> Tuple[str, Set[str]]:
    columns = get_table_columns(schema, table)
    missing = sorted(CORE_SOURCE_COLUMNS - columns)
    if missing:
        raise RuntimeError(
            f"源表 {schema}.{table} 缺少基础字段: {', '.join(missing)}；"
            f"当前字段: {', '.join(sorted(columns))}"
        )

    if "spu" in columns:
        product_expr = quote_identifier("spu")
        product_note = "spu"
    elif "sku" in columns:
        product_expr = f"SUBSTRING_INDEX({quote_identifier('sku')}, '-', 1)"
        product_note = "SUBSTRING_INDEX(sku, '-', 1)"
    else:
        raise RuntimeError(
            f"源表 {schema}.{table} 既没有 spu，也没有可用于提取款号的 sku"
        )

    logger.info(f"款号口径使用: {product_note}")
    logger.info("转化率暂不生成：源表尚无 Sessions 字段")
    return product_expr, columns


def query_metrics(
    schema: str,
    table: str,
    product_expr: str,
    spu: str,
    stores: List[str],
    start_date: date,
    end_date: date,
    country: str,
) -> Dict[str, Any]:
    placeholders = ",".join(["%s"] * len(stores))
    sql = f"""
        SELECT
            COUNT(*) AS source_rows,
            SUM(COALESCE({quote_identifier('clicks')}, 0)) AS clicks,
            SUM(COALESCE({quote_identifier('impressions')}, 0)) AS impressions,
            SUM(COALESCE({quote_identifier('ad_order_qty')}, 0)) AS ad_order_qty,
            SUM(COALESCE({quote_identifier('ad_cost_amt')}, 0)) AS ad_cost_amt
        FROM {quote_identifier(schema)}.{quote_identifier(table)}
        WHERE {quote_identifier('dt')} BETWEEN %s AND %s
          AND {product_expr} = %s
          AND {quote_identifier('store_name')} IN ({placeholders})
          AND {quote_identifier('country')} = %s
    """

    params: List[Any] = [start_date, end_date, spu, *stores, country]
    with db_cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone() or {}

    clicks = float(row.get("clicks") or 0)
    impressions = float(row.get("impressions") or 0)
    ad_order_qty = float(row.get("ad_order_qty") or 0)
    ad_cost_amt = float(row.get("ad_cost_amt") or 0)

    return {
        "source_rows": int(row.get("source_rows") or 0),
        "点击量": int(round(clicks)),
        "CTR": round(clicks / impressions, 6) if impressions else None,
        "CPC": round(ad_cost_amt / clicks, 6) if clicks else None,
        "广告转化率": round(ad_order_qty / clicks, 6) if clicks else None,
    }


async def run(args: argparse.Namespace) -> None:
    schema, table = find_table(args.source_table, args.source_schema)
    logger.info(f"数据源: {schema}.{table}")
    product_expr, _ = resolve_source_layout(schema, table)

    client = FeishuBitableClient(args.app_token)
    fields = await client.list_fields(args.table_id)
    ad_conversion_field_exists = "广告转化率" in fields

    if not ad_conversion_field_exists:
        if args.dry_run:
            logger.warning(
                "飞书表暂缺“广告转化率”字段；本次只读试跑仍会计算该指标，"
                "正式执行时将自动创建字段"
            )
        else:
            await client.create_field(args.table_id, "广告转化率", 2, precision=6)
            fields = await client.list_fields(args.table_id)
            ad_conversion_field_exists = "广告转化率" in fields

    required_fields = INPUT_FIELDS | {"CTR", "CPC", "点击量"}
    if not args.dry_run or ad_conversion_field_exists:
        required_fields.add("广告转化率")

    missing = sorted(required_fields - set(fields))
    if missing:
        raise RuntimeError(f"飞书表缺少字段: {', '.join(missing)}")

    fields_to_type_check = {"CTR", "CPC", "点击量"}
    if ad_conversion_field_exists:
        fields_to_type_check.add("广告转化率")

    wrong_types = [
        name
        for name in fields_to_type_check
        if int(fields[name].get("type") or 0) != 2
    ]
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
        if (
            not record_id
            or not spu
            or not brands
            or not start_date
            or not end_date
            or unknown
        ):
            logger.warning(
                f"跳过 {spu or record_id}: 条件不完整或店铺未映射 {unknown}"
            )
            skipped += 1
            continue

        if end_date < start_date:
            logger.warning(f"跳过 {spu}: 结束日期早于开始日期")
            skipped += 1
            continue

        stores = sorted({STORE_MAP[brand] for brand in brands})
        metrics = query_metrics(
            schema=schema,
            table=table,
            product_expr=product_expr,
            spu=spu,
            stores=stores,
            start_date=start_date,
            end_date=end_date,
            country=args.country,
        )

        output = {name: metrics[name] for name in OUTPUT_FIELDS}
        updates.append({"record_id": record_id, "fields": output})

        logger.info(
            f"{spu} | {brands}->{stores} | {start_date}~{end_date} | "
            f"rows={metrics['source_rows']} | 点击量={metrics['点击量']} | "
            f"CTR={metrics['CTR']} | CPC={metrics['CPC']} | "
            f"广告转化率={metrics['广告转化率']}"
        )

        if args.limit and len(updates) >= args.limit:
            break

    logger.info(f"生成回写结果 {len(updates)} 条，跳过 {skipped} 条")
    if args.dry_run:
        logger.info("DRY RUN：未创建字段、未修改飞书记录")
        return

    await update_records(client, args.app_token, args.table_id, updates)
    logger.info("回写完成；飞书‘转化率’字段未被修改")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填拍摄效果区间指标")
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
