#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探查飞书多维表的表信息、字段结构和样例记录。

示例：
    venv/bin/python scripts/inspect_feishu_bitable.py \
      --url 'https://example.feishu.cn/base/APP_TOKEN?table=TABLE_ID&view=VIEW_ID'

本脚本只调用飞书读取接口，不创建字段、不删除记录、不更新记录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common import FeishuBitableClient, settings  # noqa: E402


FIELD_TYPE_NAMES: Dict[int, str] = {
    1: "文本",
    2: "数字",
    3: "单选",
    4: "多选",
    5: "日期",
    7: "复选框",
    11: "人员",
    13: "电话号码",
    15: "超链接",
    17: "附件",
    18: "单向关联",
    19: "查找引用",
    20: "公式",
    21: "双向关联",
    22: "地理位置",
    23: "群组",
    1001: "创建时间",
    1002: "最后更新时间",
    1003: "创建人",
    1004: "修改人",
    1005: "自动编号",
}


def parse_bitable_url(url: str) -> Tuple[str, str, str]:
    """从飞书多维表链接解析 app_token、table_id 和 view_id。"""
    parsed = urlparse(url.strip())
    path_parts = [part for part in parsed.path.split("/") if part]

    try:
        base_index = path_parts.index("base")
        app_token = path_parts[base_index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"无法从链接解析 app_token: {url}") from exc

    query = parse_qs(parsed.query)
    table_id = (query.get("table") or [""])[0]
    view_id = (query.get("view") or [""])[0]

    if not table_id:
        raise ValueError(f"链接中未包含 table 参数: {url}")

    return app_token, table_id, view_id


def resolve_target(args: argparse.Namespace) -> Tuple[str, str, str]:
    if args.url:
        app_token, table_id, view_id = parse_bitable_url(args.url)
        return app_token, table_id, view_id

    app_token = args.app_token or settings.FEISHU_APP_TOKEN
    table_id = args.table_id
    view_id = args.view_id

    if not app_token:
        raise ValueError("缺少 app_token，请使用 --url 或 --app-token")
    if not table_id:
        raise ValueError("缺少 table_id，请使用 --url 或 --table-id")

    return app_token, table_id, view_id


async def api_get(
    client: FeishuBitableClient,
    path: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    token = await client.get_tenant_access_token()
    url = f"{client.api_base}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        response = await http_client.get(url, headers=headers, params=params)

    try:
        result = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"飞书接口返回非 JSON，HTTP {response.status_code}: {response.text[:500]}"
        ) from exc

    if result.get("code") != 0:
        raise RuntimeError(f"读取飞书接口失败: {result}")
    return result


async def get_table_name(client: FeishuBitableClient, app_token: str, table_id: str) -> str:
    result = await api_get(
        client,
        f"bitable/v1/apps/{app_token}/tables",
        params={"page_size": 100},
    )
    items = ((result.get("data") or {}).get("items") or [])
    for item in items:
        if item.get("table_id") == table_id:
            return str(item.get("name") or "")
    return ""


async def get_fields(
    client: FeishuBitableClient,
    app_token: str,
    table_id: str,
) -> List[Dict[str, Any]]:
    all_fields: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while True:
        params: Dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token

        result = await api_get(
            client,
            f"bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            params=params,
        )
        data = result.get("data") or {}
        all_fields.extend(data.get("items") or [])

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    return all_fields


async def get_sample_records(
    client: FeishuBitableClient,
    app_token: str,
    table_id: str,
    view_id: str,
    sample_size: int,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    params: Dict[str, Any] = {
        "page_size": max(1, min(sample_size, 500)),
    }
    if view_id:
        params["view_id"] = view_id

    result = await api_get(
        client,
        f"bitable/v1/apps/{app_token}/tables/{table_id}/records",
        params=params,
    )
    data = result.get("data") or {}
    records = data.get("items") or []
    total = data.get("total")
    return records[:sample_size], total


def timestamp_to_text(value: Any) -> Any:
    """将飞书日期字段的毫秒时间戳转换为本地可读时间。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError, OverflowError):
            return value
    return value


def simplify_value(value: Any, field_type: int) -> Any:
    if value is None:
        return None
    if field_type in {5, 1001, 1002}:
        return timestamp_to_text(value)
    if isinstance(value, list):
        return [simplify_value(item, field_type) for item in value]
    if isinstance(value, dict):
        return {key: simplify_value(item, field_type) for key, item in value.items()}
    return value


def print_report(
    app_token: str,
    table_id: str,
    view_id: str,
    table_name: str,
    fields: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    total: Optional[int],
    raw_json: bool,
) -> None:
    print("=" * 100)
    print("飞书多维表只读探查结果")
    print("=" * 100)
    print(f"app_token : {app_token}")
    print(f"table_id  : {table_id}")
    print(f"view_id   : {view_id or '未指定'}")
    print(f"表名称    : {table_name or '未识别'}")
    print(f"字段数    : {len(fields)}")
    print(f"记录总数  : {total if total is not None else '接口未返回'}")

    print("\n" + "-" * 100)
    print("字段结构")
    print("-" * 100)
    print(f"{'序号':<6}{'字段名':<28}{'类型':<18}{'字段ID':<18}{'主字段':<8}")

    field_type_by_name: Dict[str, int] = {}
    for index, field in enumerate(fields, start=1):
        field_name = str(field.get("field_name") or "")
        field_type = int(field.get("type") or 0)
        field_id = str(field.get("field_id") or "")
        is_primary = bool(field.get("is_primary"))
        type_name = FIELD_TYPE_NAMES.get(field_type, f"未知类型({field_type})")
        field_type_by_name[field_name] = field_type
        print(f"{index:<6}{field_name:<28}{type_name:<18}{field_id:<18}{'是' if is_primary else '否':<8}")

        property_value = field.get("property")
        if property_value:
            print(" " * 6 + "property: " + json.dumps(property_value, ensure_ascii=False, default=str))

    print("\n" + "-" * 100)
    print(f"样例记录（前 {len(records)} 条）")
    print("-" * 100)

    if not records:
        print("当前视图没有返回记录。")
    else:
        for index, record in enumerate(records, start=1):
            record_id = record.get("record_id") or record.get("id") or ""
            print(f"\n[{index}] record_id={record_id}")
            record_fields = record.get("fields") or {}
            for field_name, value in record_fields.items():
                field_type = field_type_by_name.get(field_name, 0)
                display_value = simplify_value(value, field_type)
                print(f"  {field_name}: {json.dumps(display_value, ensure_ascii=False, default=str)}")

    if raw_json:
        print("\n" + "-" * 100)
        print("原始字段 JSON")
        print("-" * 100)
        print(json.dumps(fields, ensure_ascii=False, indent=2, default=str))
        print("\n" + "-" * 100)
        print("原始记录 JSON")
        print("-" * 100)
        print(json.dumps(records, ensure_ascii=False, indent=2, default=str))

    print("\n" + "=" * 100)
    print("探查完成：本脚本未创建字段、未修改记录、未删除记录。")
    print("=" * 100)


async def async_main(args: argparse.Namespace) -> None:
    app_token, table_id, view_id = resolve_target(args)
    client = FeishuBitableClient(app_token)

    table_name, fields, record_result = await asyncio.gather(
        get_table_name(client, app_token, table_id),
        get_fields(client, app_token, table_id),
        get_sample_records(client, app_token, table_id, view_id, args.sample_size),
    )
    records, total = record_result

    print_report(
        app_token=app_token,
        table_id=table_id,
        view_id=view_id,
        table_name=table_name,
        fields=fields,
        records=records,
        total=total,
        raw_json=args.raw_json,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读探查飞书多维表字段和样例记录")
    parser.add_argument("--url", default="", help="飞书多维表链接，可自动解析 app_token/table_id/view_id")
    parser.add_argument("--app-token", default="", help="多维表 app_token；使用 --url 时无需填写")
    parser.add_argument("--table-id", default="", help="数据表 table_id；使用 --url 时无需填写")
    parser.add_argument("--view-id", default="", help="视图 view_id，可选")
    parser.add_argument("--sample-size", type=int, default=10, help="读取样例记录数量，默认10，最大500")
    parser.add_argument("--raw-json", action="store_true", help="额外输出飞书接口原始字段和记录 JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.sample_size < 1 or args.sample_size > 500:
        raise SystemExit("--sample-size 必须在 1 到 500 之间")
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
