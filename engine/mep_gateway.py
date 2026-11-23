#!/usr/bin/env python3
"""
VideoOps-MEP 1.0 确定性风控与执行网关（零第三方依赖）
实现：幂等防重 / revision 乐观锁 / 积分预算熔断 / 白名单事务执行 / 自动快照与整单回滚
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ALLOWED_OPS = {
    "submit_generation_batch",
    "review_mark",
    "publish_selected",
    "archive_rejected",
    "rollback",
}

ACCENT_PALETTE = ["#B5835A", "#6D8B74", "#5F7A95", "#9B6B7C", "#C29B59", "#6F7394"]


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class MEPGateway:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.state_file = workspace_dir / "state.json"
        self.requests_dir = workspace_dir / "requests"
        self.results_dir = workspace_dir / "results"
        self.snapshots_dir = workspace_dir / "snapshots"
        for d in (self.requests_dir, self.results_dir, self.snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.seed_backup = workspace_dir / ".seed_state.json"
        if self.state_file.exists() and not self.seed_backup.exists():
            self.seed_backup.write_text(self.state_file.read_text(encoding="utf-8"), encoding="utf-8")

    def load_state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def reset_demo(self) -> dict[str, Any]:
        if self.seed_backup.exists():
            seed = json.loads(self.seed_backup.read_text(encoding="utf-8"))
            self.save_state(seed)
        for d in (self.requests_dir, self.results_dir, self.snapshots_dir):
            for f in d.glob("*.json"):
                f.unlink(missing_ok=True)
        return self.load_state()

    def _write_result(self, req_id: str, result: dict[str, Any]) -> None:
        out_path = self.results_dir / f"{req_id}.result.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def _reject(
        self,
        state: dict[str, Any],
        req: dict[str, Any],
        code: str,
        reason: str,
        next_action: str,
        saved_waste: int = 0,
    ) -> dict[str, Any]:
        req_id = req.get("request_id", f"req_{int(dt.datetime.now().timestamp())}")
        actor = req.get("actor", "Unknown")
        state["account"]["blocked_incidents"] = state["account"].get("blocked_incidents", 0) + 1
        if saved_waste > 0:
            state["account"]["blocked_waste_credits"] = (
                state["account"].get("blocked_waste_credits", 0) + saved_waste
            )
        state["audit_log"].insert(
            0,
            {
                "request_id": req_id,
                "actor": actor,
                "ok": False,
                "error_code": code,
                "summary": f"拦截 [{code}]：{reason}",
                "saved_credits": saved_waste,
                "revision": state["revision"],
                "snapshot_id": None,
                "at": now_iso(),
            },
        )
        state["audit_log"] = state["audit_log"][:30]
        self.save_state(state)

        result = {
            "mep": "video-ops/1.0",
            "request_id": req_id,
            "ok": False,
            "error_code": code,
            "reason": reason,
            "next_action": next_action,
            "credits_charged": 0,
            "saved_waste_credits": saved_waste,
            "revision": state["revision"],
            "at": now_iso(),
        }
        self._write_result(req_id, result)
        return result

    def process_request(self, req: dict[str, Any]) -> dict[str, Any]:
        state = self.load_state()
        req_id = req.get("request_id") or f"req_{dt.datetime.now().strftime('%H%M%S')}"
        req["request_id"] = req_id
        actor = req.get("actor", "AI-Agent")

        # 落盘真实请求单，体现文件级协议可审计性
        req_path = self.requests_dir / f"{req_id}.json"
        req_path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")

        ops = req.get("ops")
        if not isinstance(ops, list) or not ops:
            return self._reject(
                state, req, "ERR_INVALID_OP", "请求缺少有效的 ops 列表", "请提供至少一个声明式操作"
            )

        # 校验 0：白名单操作类型
        for op_obj in ops:
            if op_obj.get("op") not in ALLOWED_OPS:
                return self._reject(
                    state,
                    req,
                    "ERR_INVALID_OP",
                    f"拒绝未授权操作：{op_obj.get('op')}",
                    f"仅允许白名单动作：{', '.join(sorted(ALLOWED_OPS))}",
                )

        # 校验 1：幂等键防重（避免轮询/重试重复扣费）
        idem = req.get("idempotency_key", "")
        est_cost = self._estimate_cost(ops)
        if idem and idem in state.get("processed_idempotency_keys", []):
            return self._reject(
                state,
                req,
                "ERR_IDEMPOTENCY_DUPLICATE",
                f"幂等键 {idem} 已受理过，阻止重复提交（拦截潜在误扣 {est_cost} 积分）",
                "请先读取 results/ 历史回执确认状态，如需新批次请生成新 idempotency_key",
                saved_waste=est_cost,
            )

        # 校验 2：revision 乐观锁（防止覆盖人工刚在对照台做的标记）
        is_rollback_only = len(ops) == 1 and ops[0].get("op") == "rollback"
        base_rev = req.get("base_revision")
        if not is_rollback_only and base_rev != state["revision"]:
            return self._reject(
                state,
                req,
                "ERR_REVISION_CONFLICT",
                f"版本冲突：请求基于 rev.{base_rev}，但工作台已更新至 rev.{state['revision']}（可能人工刚改过筛选状态）",
                "请先重新读取 workspace/state.json 获取最新状态与版本号后再提交",
                saved_waste=est_cost,
            )

        # 校验 3：积分预算熔断（单单硬上限 + 请求自设预算 + 账户余额）
        max_policy = state["policy"].get("max_credits_per_request", 120)
        req_budget = req.get("max_credit_budget", max_policy)
        balance = state["account"].get("credits_balance", 0)
        if est_cost > max_policy or est_cost > req_budget:
            limit = min(max_policy, req_budget)
            return self._reject(
                state,
                req,
                "ERR_BUDGET_EXCEEDED",
                f"触发单单预算熔断：本单预估消耗 {est_cost} 积分，超出安全上限 {limit} 积分",
                "请拆分小批量或由人工显式调高策略上限后重试",
                saved_waste=est_cost,
            )
        if est_cost > balance:
            return self._reject(
                state,
                req,
                "ERR_INSUFFICIENT_CREDITS",
                f"积分余额不足：需 {est_cost} 积分，当前仅剩 {balance} 积分",
                "请缩减批次数量或充值后再提交",
                saved_waste=est_cost,
            )

        # 校验通过：先拍自动快照（支撑整单原子回滚）
        old_rev = state["revision"]
        snap_id = f"snap_rev{old_rev}_{req_id}"
        if not is_rollback_only:
            snap_path = self.snapshots_dir / f"{snap_id}.json"
            snap_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        new_state = copy.deepcopy(state)
        changes: list[str] = []
        charged = 0

        for op_obj in ops:
            op = op_obj["op"]
            if op == "rollback":
                target_snap = op_obj.get("snapshot_id")
                snap_file = self.snapshots_dir / f"{target_snap}.json"
                if not target_snap or not snap_file.exists():
                    return self._reject(
                        state, req, "ERR_SNAPSHOT_NOT_FOUND", f"找不到快照 {target_snap}", "请检查有效 snapshot_id"
                    )
                restored = json.loads(snap_file.read_text(encoding="utf-8"))
                restored["revision"] = old_rev + 1
                restored["updated_at"] = now_iso()
                restored["audit_log"].insert(
                    0,
                    {
                        "request_id": req_id,
                        "actor": actor,
                        "ok": True,
                        "summary": f"整单回滚至快照 {target_snap}（积分与作品状态已原子恢复）",
                        "revision": restored["revision"],
                        "snapshot_id": None,
                        "at": now_iso(),
                    },
                )
                self.save_state(restored)
                result = {
                    "mep": "video-ops/1.0",
                    "request_id": req_id,
                    "ok": True,
                    "credits_charged": 0,
                    "old_revision": old_rev,
                    "new_revision": restored["revision"],
                    "changes": [f"已原子回滚至 {target_snap}"],
                    "at": now_iso(),
                }
                self._write_result(req_id, result)
                return result

            elif op == "submit_generation_batch":
                items = op_obj.get("items", [])
                cost_each = int(op_obj.get("cost_per_item", 20))
                model = op_obj.get("model", "kling-4.0")
                tpl = op_obj.get("template_id", "tpl_fashion_runway_v2")
                next_num = 101 + len(new_state["assets"])
                for idx, it in enumerate(items):
                    vid_id = f"VID-{next_num + idx}"
                    new_state["assets"].insert(
                        0,
                        {
                            "id": vid_id,
                            "sku": it.get("sku", f"SKU-{next_num + idx}"),
                            "title": it.get("title", "未命名视频"),
                            "model": model,
                            "template_id": tpl,
                            "status": "review_pending",
                            "quality_score": round(8.2 + (idx % 3) * 0.4, 1),
                            "cost": cost_each,
                            "accent": ACCENT_PALETTE[(next_num + idx) % len(ACCENT_PALETTE)],
                            "note": it.get("prompt_summary", "由 MEP 批处理单提交，待人工对照复核"),
                        },
                    )
                    charged += cost_each
                    changes.append(f"生成 {vid_id} ({it.get('title','')}) · {model} · -{cost_each} 积分")

            elif op == "review_mark":
                asset_id = op_obj.get("asset_id")
                target_status = op_obj.get("status")
                note = op_obj.get("note", "")
                for a in new_state["assets"]:
                    if a["id"] == asset_id:
                        old_st = a["status"]
                        a["status"] = target_status
                        if note:
                            a["note"] = note
                        changes.append(f"{asset_id} 状态 {old_st} → {target_status}")
                        break

            elif op == "publish_selected":
                ids = set(op_obj.get("asset_ids", []))
                require_review = new_state["policy"].get("require_human_review_before_publish", True)
                for a in new_state["assets"]:
                    if a["id"] in ids:
                        if require_review and a["status"] != "approved":
                            return self._reject(
                                state,
                                req,
                                "ERR_UNAPPROVED_PUBLISH",
                                f"禁止直接上架未通过人工复核的作品：{a['id']}（当前状态 {a['status']}）",
                                "请先经人工对照页标记为 approved 后再提交上架单",
                            )
                        a["status"] = "published"
                        changes.append(f"上架 {a['id']} ({a['title']})")

            elif op == "archive_rejected":
                ids = set(op_obj.get("asset_ids", []))
                for a in new_state["assets"]:
                    if a["id"] in ids:
                        a["status"] = "archived"
                        changes.append(f"下架归档 {a['id']}")

        new_state["account"]["credits_balance"] -= charged
        new_state["account"]["credits_spent_total"] += charged
        if idem:
            new_state.setdefault("processed_idempotency_keys", []).append(idem)
        new_state["revision"] = old_rev + 1
        new_state["updated_at"] = now_iso()
        new_state["audit_log"].insert(
            0,
            {
                "request_id": req_id,
                "actor": actor,
                "ok": True,
                "summary": "；".join(changes[:3]) + (f"（共 {len(changes)} 项）" if len(changes) > 3 else ""),
                "credits_charged": charged,
                "revision": new_state["revision"],
                "snapshot_id": snap_id,
                "at": now_iso(),
            },
        )
        new_state["audit_log"] = new_state["audit_log"][:30]
        self.save_state(new_state)

        result = {
            "mep": "video-ops/1.0",
            "request_id": req_id,
            "ok": True,
            "credits_charged": charged,
            "old_revision": old_rev,
            "new_revision": new_state["revision"],
            "snapshot_id": snap_id,
            "changes": changes,
            "at": now_iso(),
        }
        self._write_result(req_id, result)
        return result

    @staticmethod
    def _estimate_cost(ops: list[dict[str, Any]]) -> int:
        total = 0
        for op_obj in ops:
            if op_obj.get("op") == "submit_generation_batch":
                total += len(op_obj.get("items", [])) * int(op_obj.get("cost_per_item", 20))
        return total


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "workspace"
    gw = MEPGateway(root)
    if len(sys.argv) < 2:
        print("用法: python3 engine/mep_gateway.py <request.json>")
        return 1
    req = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    res = gw.process_request(req)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
