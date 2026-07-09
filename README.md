# lx-feishu-bitable

把 ODS MySQL 表 `ods_lx_product_performance` 同步到飞书多维表 `REclw3BsTiCMFcknZfIcdbySn1b`。

当前任务包含两个目标数据表：

| 目标表 | 数据窗口 | 说明 |
|---|---:|---|
| 90天数据 | 90天 | 从源表日期字段取最近90天 |
| 7天数据 | 7天 | 从源表日期字段取最近7天 |

默认窗口结束日期使用源表 `MAX(dt)`，不是服务器当天日期。这样即使 ODS 数据延迟，也不会因为当天无数据导致同步为空。

## 项目结构

```text
lx-feishu-bitable/
├── common/
│   ├── config.py              # .env 配置读取
│   ├── database.py            # MySQL 连接与安全表名/字段名处理
│   ├── feishu_bitable.py      # 飞书多维表 OpenAPI 客户端
│   └── logger.py              # 日志
├── jobs/
│   └── sync_product_performance.py
├── scripts/
│   ├── setup_server.sh
│   └── run_sync_product_performance.sh
├── config.example.env
├── requirements.txt
└── README.md
```

## 服务器部署

```bash
cd /opt/apps
git clone https://github.com/Digital-sy/lx-feishu-bitable.git
cd /opt/apps/lx-feishu-bitable
bash scripts/setup_server.sh
```

然后编辑 `.env`：

```bash
vim /opt/apps/lx-feishu-bitable/.env
```

重点配置：

```env
DB_HOST=你的数据库地址
DB_PORT=3306
DB_USER=你的数据库用户
DB_PASSWORD=你的数据库密码
DB_DATABASE=ods

FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_APP_TOKEN=REclw3BsTiCMFcknZfIcdbySn1b

# 已知道 table_id 就填 table_id；不知道就填表名，让脚本自动解析
FEISHU_90D_TABLE_ID=
FEISHU_90D_TABLE_NAME=90天数据
FEISHU_7D_TABLE_ID=
FEISHU_7D_TABLE_NAME=7天数据
```

## 先做 dry-run

```bash
cd /opt/apps/lx-feishu-bitable
bash scripts/run_sync_product_performance.sh --dry-run
```

`dry-run` 只会：

1. 校验 `.env`；
2. 连接数据库；
3. 检查源表字段；
4. 获取飞书 app 下的数据表；
5. 统计 90 天和 7 天待写入行数。

不会清空飞书表，也不会写入数据。

## 正式同步

```bash
cd /opt/apps/lx-feishu-bitable
bash scripts/run_sync_product_performance.sh
```

执行逻辑：

1. 读取源表字段；
2. 自动在飞书目标表创建缺失字段；
3. 读取最近 90 天数据，刷新 90 天表；
4. 读取最近 7 天数据，刷新 7 天表；
5. 每次刷新采用“清空旧数据 → 批量写入新数据”的全量覆盖方式。

## 定时任务示例

每天凌晨 03:30 同步：

```bash
crontab -e
```

加入：

```cron
30 3 * * * cd /opt/apps/lx-feishu-bitable && bash scripts/run_sync_product_performance.sh >> logs/cron_sync_product_performance.log 2>&1
```

## 注意事项

1. 飞书应用需要有多维表格读写权限，并且应用要被添加到目标多维表。
2. `FEISHU_90D_TABLE_NAME` / `FEISHU_7D_TABLE_NAME` 必须和飞书里的数据表名称完全一致；更稳妥的方式是直接配置 table_id。
3. 默认 `MAX_FEISHU_RECORDS=0`，表示不做本地行数限制。若你的飞书多维表容量是 20,000 行，可以改为 `MAX_FEISHU_RECORDS=20000`，避免清空后写入失败。
4. 源表名和日期字段只允许字母、数字、下划线，避免误配置导致 SQL 注入风险。
