#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run shooting metrics refresh and send a Feishu completion card."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import FeishuBitableClient, get_logger  # noqa: E402
from common.feishu_message import send_interactive_card  # noqa: E402

logger = get_logger("run_shooting_metrics_daily")

SUMMARY_RE = re.compile(r"生成回写结果\s+(\d+)\s+条，跳过\s+(\d+)\s+条")
SOURCE_DATE_RE = re.compile(r"源表最新日期:\s*([^\s]+)")
COUNTRY_RE = re.compile(r"国家过滤值:\s*([^\s]+)\s*->\s*([^\s]+)")
UPDATED_RE = re.compile(r"已更新\s+(\d+)/(\d+)")


def build_job_command(args: argparse.Namespace) -> List[str]:
    command = [
        sys.executable,
        str(ROOT / "jobs" / "fill_shooting_period_metrics.py"),
        "--app-token",
        args.app_token,
        "--table-id",
        args.table_id,
        "--source-schema",
        args.source_schema,
        "--source-table",
        args.source_table,
        "--country",
        args.country,
    ]
    if args.view_id:
        command.extend(["--view-id", args.view_id])
    if args.as_of_date:
        command.extend(["--as-of-date", args.as_of_date])
    if args.limit:
        command.extend(["--limit", str(args.limit)])
    if args.dry_run:
        command.append("--dry-run")
    return command


def run_job(command: List[str]) -> Tuple[int, List[str]]:
    logger.info("开始执行拍摄效果指标回写")
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: List[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        text_line = line.rstrip("\n")
        lines.append(text_line)
        print(text_line, flush=True)
    return_code = process.wait()
    return return_code, lines


def parse_summary(lines: List[str]) -> Dict[str, str]:
    result: Dict[str, str] = {
        "generated": "0",
        "skipped": "0",
        "updated": "0",
        "source_date": "未知",
        "country": "未知",
    }
    for line in lines:
        match = SUMMARY_RE.search(line)
        if match:
            result["generated"], result["skipped"] = match.groups()
        match = SOURCE_DATE_RE.search(line)
        if match:
            result["source_date"] = match.group(1)
        match = COUNTRY_RE.search(line)
        if match:
            result["country"] = match.group(2)
        match = UPDATED_RE.search(line)
        if match:
            result["updated"] = match.group(1)

    if result["updated"] == "0":
        result["updated"] = result["generated"]
    return result


def build_card(
    args: argparse.Namespace,
    summary: Dict[str, str],
    finished_at: datetime,
) -> Dict[str, object]:
    run_date = args.as_of_date or date.today().isoformat()
    table_url = (
        f"https://yxqje9jjxtk.feishu.cn/base/{args.app_token}"
        f"?table={args.table_id}"
        + (f"&view={args.view_id}" if args.view_id else "")
    )
    content = (
        f"**执行状态：** ✅ 已完成\n"
        f"**运行基准日：** {run_date}\n"
        f"**源表最新日期：** {summary['source_date']}\n"
        f"**国家：** {summary['country']}\n"
        f"**生成回写：** {summary['generated']} 条\n"
        f"**成功更新：** {summary['updated']} 条\n"
        f"**跳过：** {summary['skipped']} 条\n"
        f"**完成时间：** {finished_at.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {
                "tag": "plain_text",
                "content": "拍摄效果跟踪｜每日数据更新完成",
            },
        },
        "elements": [
            {"tag": "markdown", "content": content},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看多维表"},
                        "type": "primary",
                        "url": table_url,
                    }
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "接收人：刘宗霖｜转化率暂未生成（DWS 尚无 Sessions）",
                    }
                ],
            },
        ],
    }


async def send_completion_card(
    args: argparse.Namespace,
    summary: Dict[str, str],
) -> None:
    receive_id = (
        args.notify_receive_id
        or os.getenv("FEISHU_SHOOTING_NOTIFY_RECEIVE_ID", "")
    ).strip()
    receive_id_type = (
        args.notify_receive_id_type
        or os.getenv("FEISHU_SHOOTING_NOTIFY_RECEIVE_ID_TYPE", "open_id")
    ).strip()

    if not receive_id:
        logger.warning(
            "数据回写已完成，但未配置刘宗霖的飞书接收 ID；"
            "请设置 FEISHU_SHOOTING_NOTIFY_RECEIVE_ID"
        )
        return

    client = FeishuBitableClient(args.app_token)
    card = build_card(args, summary, datetime.now())
    await send_interactive_card(
        client=client,
        receive_id=receive_id,
        receive_id_type=receive_id_type,
        card=card,
    )


async def async_main(args: argparse.Namespace) -> int:
    command = build_job_command(args)
    return_code, lines = run_job(command)
    if return_code != 0:
        logger.error(f"拍摄效果指标回写失败，退出码: {return_code}；不发送成功卡片")
        return return_code

    summary = parse_summary(lines)
    if args.dry_run:
        logger.info("DRY RUN 完成，不发送飞书卡片")
        return 0

    try:
        await send_completion_card(args, summary)
    except Exception as exc:
        logger.error(f"数据回写成功，但飞书完成卡片发送失败: {exc}")
        return 2 if args.notification_required else 0

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日回写拍摄效果指标并发送飞书完成卡片")
    parser.add_argument("--app-token", required=True)
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--view-id", default="")
    parser.add_argument("--source-schema", default="dws_db")
    parser.add_argument("--source-table", default="dws_op_listing_traffic_daily")
    parser.add_argument("--country", default="US")
    parser.add_argument("--as-of-date", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--notify-receive-id",
        default="",
        help="刘宗霖的飞书接收 ID；默认读取 FEISHU_SHOOTING_NOTIFY_RECEIVE_ID",
    )
    parser.add_argument(
        "--notify-receive-id-type",
        default="",
        choices=["", "open_id", "user_id", "union_id", "email", "chat_id"],
        help="接收 ID 类型；默认读取环境变量，未配置时使用 open_id",
    )
    parser.add_argument(
        "--notification-required",
        action="store_true",
        help="卡片发送失败时让任务以退出码 2 失败",
    )
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(async_main(parse_args())))


if __name__ == "__main__":
    main()
