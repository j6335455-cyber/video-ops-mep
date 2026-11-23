# VideoOps-MEP 1.0 协议规范（面向 AIGC 批量内容生产的确定性协作协议）

> **设计灵感**：借鉴开源 [MEP (Motion Edit Protocol)](https://github.com/adunext/mep) 的「文件夹即工作台」范式，将其从单机视频剪辑迁移到 **多模型 AIGC 批量内容生产链路（生成 → 人工可控筛选 → 模板复用 → 发布上线）**。

## 1. 核心定位：AI 开处方，确定性网关动刀

| 维度 | 传统 Computer Use（点界面） | 传统 MCP（工具直调） | **VideoOps-MEP（文件级受控工作台）** |
| :--- | :--- | :--- | :--- |
| 接入成本 | 易受 UI 改动影响 | 需配 Server、重启客户端 | **零插件、零配置**，任何 AI 读写文件夹即接入 |
| 上下文消耗 | 持续截屏，高消耗 | 工具 Schema 常驻占 Token | **闲置 0 Token**，执行时按需读 `state.json` |
| 成本与风控 | 易重复点击导致误扣费 | 缺乏统一事务与预算硬熔断 | **幂等键 + 单单预算上限 + 余额熔断** |
| 人机协同冲突 | AI 易覆盖人工刚做的筛选 | 缺少版本锁 | **`revision` 乐观锁**，冲突即拒绝并提示重读 |
| 容错与审计 | 改乱难恢复 | 零散调用难回滚 | **每单自动快照 + 结构化回执 + 整单 `rollback`** |

## 2. 目录结构约定

```text
workspace/
├── state.json       # 当前唯一真相源（含 revision 版本号、积分预算、模板库、任务与作品列表）
├── requests/        # AI 投递的声明式 JSON 批处理单据（如 req_001.json）
├── results/         # 网关校验并执行后写入的回执（如 req_001.result.json）
└── snapshots/       # 每次变更前自动生成的全量快照（供一键 rollback 整单回滚）
```

## 3. 请求单据 Schema（`requests/<id>.json`）

```json
{
  "mep": "video-ops/1.0",
  "request_id": "req_20261121_001",
  "idempotency_key": "idem_batch09_kling4_001",
  "actor": "Codex / Antigravity / Human",
  "base_revision": 3,
  "max_credit_budget": 120,
  "ops": [
    {
      "op": "submit_generation_batch",
      "template_id": "tpl_fashion_runway_v2",
      "model": "kling-4.0",
      "resolution": "720p",
      "cost_per_item": 20,
      "items": [
        { "sku": "SKU-041", "title": "法式通勤风衣 · 街拍回眸", "prompt_summary": "微风自然光、中景转特写" }
      ]
    }
  ]
}
```

## 4. 四道确定性安全护栏（校验顺序）

1. **`ERR_IDEMPOTENCY_DUPLICATE`（幂等防重）**：相同 `idempotency_key` 已被成功受理过，直接拒绝重复提交，防止自动化轮询重复扣费。
2. **`ERR_REVISION_CONFLICT`（乐观锁防撞车）**：`base_revision !== state.revision`，说明人工或其他 Agent 已更新状态（如在对照页新标了“重制”），强制拦截并提示先重读 `state.json`。
3. **`ERR_BUDGET_EXCEEDED` / `ERR_INSUFFICIENT_CREDITS`（积分熔断）**：预估总消耗超过请求自设 `max_credit_budget`、全局单单硬上限（`policy.max_credits_per_request`）或账户剩余积分，整单原子拦截，**0 扣费**。
4. **`ERR_INVALID_OP`（规则白名单校验）**：仅允许白名单内的声明式动作（`submit_generation_batch` / `review_mark` / `publish_selected` / `archive_rejected` / `rollback`），禁止任意越权写操作。

## 5. 回执 Schema（`results/<id>.result.json`）

每张单据无论成功或被拦截，均输出标准化回执（含 `ok`、`error_code`、`reason`、`next_action`、`credits_charged`、`old_revision`、`new_revision`、`snapshot_id`、`changes`），形成完整可审计、可回滚的闭环。
