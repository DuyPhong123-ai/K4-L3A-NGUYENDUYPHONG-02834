from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.workflow import coordinate_case, solve_case


class RecordingTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        self.calls.append(tool_name)
        data: Any
        domain = "payment"
        if tool_name == "get_order":
            domain = "order"
            data = {"order_id": arguments["order_id"], "order_status": "canceled"}
        elif tool_name == "get_order_payments":
            data = [{"order_id": arguments["order_id"], "payment_value": "79.00"}]
        elif tool_name == "get_payment_timeline":
            data = {"order_id": arguments["order_id"], "events": []}
        elif tool_name == "get_policy":
            domain = "policy"
            data = {
                "rules": {
                    "canceled_order_paid": {
                        "case_status": "action_required",
                        "recommended_action": "issue_refund",
                        "refund_brl": 79.0,
                        "responsible_parties": [
                            {"party_type": "platform", "party_id": None}
                        ],
                    }
                }
            }
        else:  # pragma: no cover - guards the fixture itself
            raise AssertionError(f"unexpected tool: {tool_name}")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name.replace('_', ''):0<24}",
            "result_hash": f"sha256:{'0' * 64}",
            "domain": domain,
            "data": data,
            "warnings": [],
        }


def sample_case() -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_001",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-001",
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
    }


def test_coordinate_case_routes_every_claim() -> None:
    trace = RecordingTrace()
    plan = coordinate_case(sample_case(), trace)  # type: ignore[arg-type]

    assert plan["primary_issue"] == "canceled_order_paid"
    assert plan["order_id"] == "order-001"
    assert [event["target"] for event in trace.events] == [
        "order-item-agent",
        "payment-agent",
        "policy-agent",
    ]


def test_solve_case_builds_schema_valid_evidence_backed_output() -> None:
    trace = RecordingTrace()
    gateway = FakeGateway()

    output = asyncio.run(
        solve_case(sample_case(), gateway, trace)  # type: ignore[arg-type]
    )

    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    contracts.validate_output(output, "test output")
    assert gateway.calls == [
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    ]
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["claim_assessments"][1]["verdict"] == "supported"
    assert any(event["event_type"] == "handoff" for event in trace.events)
    assert any(event["event_type"] == "verification_completed" for event in trace.events)
