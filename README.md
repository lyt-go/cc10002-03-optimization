# 数仓分层建模与治理平台

面向原始数据落地、清洗转换、分层建模、维度事实设计、分区治理、质量校验、血缘追踪与历史补数的数据仓库工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 原始落地 | `landing` | 落地批次 | 批次编号、来源系统、落地路径 |
| 贴源层 | `ods` | 贴源表 | 表编号、表名称、来源系统 |
| 明细层 | `dwd` | 明细表 | 表编号、表名称、业务域 || 维度层 | `dim` | 维度表 | 维度编号、维度名称、自然键列 |
| 汇总层 | `dws` | 汇总表 | 表编号、表名称、统计粒度 |
| 应用层 | `ads` | 应用表 | 表编号、表名称、业务场景 |
| 分区治理 | `partition` | 分区方案 | 方案编号、数据表名、分区字段 |
| 质量校验 | `quality` | 质量规则 | 规则编号、校验表名、目标字段 |
| 数据血缘 | `lineage` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 历史补数 | `backfill` | 补数任务 | 任务编号、目标表名、开始日期 |
| 清洗规则 | `cleanrule` | 清洗规则 | 规则编号、输入表名、空值策略 |
| 转换编排 | `transform` | 转换节点 | 节点编号、输入表名、输出表名 |
| 数据字典 | `dictionary` | 字典条目 | 条目编号、表名、字段名称 |
| 元数据管理 | `metamodel` | 元数据项 | 元数据编号、对象类型、对象名称 |
| 数据生命周期 | `lifecycle` | 生命周期策略 | 策略编号、数据表名、保留天数 |
| 存储成本 | `cost` | 成本账单 | 账单编号、数据表名、存储大小GB |
| 同步通道 | `sync` | 同步通道 | 通道编号、来源表名、目标表名 |
| 变更审计 | `auditlog` | 变更记录 | 记录编号、操作表名、变更类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 贴源审批凭证（ODS → DWD 数据流）

贴源表进入建表审批时，`app/services/ods_credential.py` 会按
「来源系统 + 增量字段 + 全量快照策略 + 最近加载时间」签发一张准入凭证，台账存在
`ods_credential`、诊断记录存在 `ods_diagnostic`（两者不计入运营概览的业务模块）。

- **复用**：四项配置未变且凭证仍在有效期内时，重复审批直接复用原凭证，不重新计算。
- **失效**：任一字段变化，当前凭证据此立即失效；需按新配置重新签发才会重新可准入。
- **同一结论**：明细层的上游准入（`/api/dwd/upstream-options`、明细列表、明细详情）
  与贴源表审批详情（`/api/ods/{id}/approval`）都走 `OdsApprovalFacade` 读取同一份凭证。
- **下线**：贴源表下线会作废凭证，明细登记保留历史定义、显示不可用原因，并标记受影响；
  贴源恢复准入后标记自动恢复。上游不可用时明细表不允许提交审批或上线。
- **争用**：同一贴源表按审核单落库顺序（凭证 id 升序）只保留一张生效凭证；
  并行写入时以最后落库的版本为准，历史凭证保留在台账里。
- **诊断**：同一配置在一个诊断周期（默认 24h，可由 `/review` 的 `period_seconds` 指定）
  内重复审批只留一条诊断记录并累加复用次数。

示例数据里的 `ODS_SALES`（来源系统 `CRM`、增量字段 `updated_at`、每日 `230000` 行、
最近加载 `2026-10-12 10:00`）可直接用来验证：调用
`POST /api/ods/4/review`（表体带 `now` 可复现时间点）看复用结果，再用
`PUT /api/ods/4` 修改快照策略，立刻会看到审批详情与明细登记都变成不可准入。

回归测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`。

领域：数据工程
子方向：数仓与数据湖
