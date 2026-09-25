from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

TOPIC_ROUTES: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order-item-agent", "payment-agent"),
    "unavailable_order_paid": ("order-item-agent", "payment-agent"),
    "late_delivery_seller": ("shipment-agent", "order-item-agent"),
    "late_delivery_logistics": ("shipment-agent",),
    "valid_split_payment": ("payment-agent",),
    "payment_mismatch": ("payment-agent", "order-item-agent"),
    "duplicate_charge": ("payment-agent",),
    "refund_pending": ("payment-agent",),
    "refund_failed": ("payment-agent",),
    "requested_full_refund": ("policy-agent",),
    "unsupported_claim": ("verifier",),
}

ISSUE_TOOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "canceled_order_paid": (
        ("order-item-agent", "get_order"),
        ("payment-agent", "get_order_payments"),
        ("payment-agent", "get_payment_timeline"),
    ),
    "unavailable_order_paid": (
        ("order-item-agent", "get_order"),
        ("order-item-agent", "get_order_items"),
        ("payment-agent", "get_order_payments"),
    ),
    "late_delivery_seller": (
        ("shipment-agent", "get_shipment_summary"),
        ("order-item-agent", "get_order_items"),
        ("order-item-agent", "get_sellers"),
    ),
    "late_delivery_logistics": (
        ("shipment-agent", "get_shipment_summary"),
        ("order-item-agent", "get_order_items"),
    ),
    "valid_split_payment": (
        ("payment-agent", "get_order_payments"),
        ("payment-agent", "get_payment_timeline"),
    ),
    "payment_mismatch": (
        ("payment-agent", "get_order_payments"),
        ("payment-agent", "get_payment_timeline"),
        ("order-item-agent", "get_order_items"),
    ),
    "duplicate_charge": (
        ("payment-agent", "get_order_payments"),
        ("payment-agent", "get_payment_timeline"),
    ),
    "refund_pending": (
        ("payment-agent", "get_order_payments"),
        ("payment-agent", "get_payment_timeline"),
        ("payment-agent", "get_refund_timeline"),
    ),
    "refund_failed": (
        ("payment-agent", "get_order_payments"),
        ("payment-agent", "get_payment_timeline"),
        ("payment-agent", "get_refund_timeline"),
    ),
    "unsupported_claim": (("order-item-agent", "get_order"),),
}


def coordinate_case(case: dict[str, Any], trace: TraceWriter) -> dict[str, Any]:
    case_id = _required_string(case, "case_id")
    request = case.get("customer_request")
    if not isinstance(request, dict):
        raise ValueError(f"case {case_id} has no valid customer_request")
    order_id = _required_string(request, "claimed_order_id")
    claims = request.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError(f"case {case_id} has no valid claims")

    normalized_claims: list[dict[str, str]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError(f"case {case_id} contains an invalid claim")
        claim_id = _required_string(claim, "claim_id")
        topic = _required_string(claim, "topic")
        normalized_claims.append({"claim_id": claim_id, "topic": topic})
        for target in TOPIC_ROUTES.get(topic, ("verifier",)):
            trace.emit(
                case_id=case_id,
                event_type="task_assigned",
                actor="coordinator",
                target=target,
                attributes={"claim_id": claim_id, "topic": topic},
            )

    primary_issue = normalized_claims[0]["topic"]
    if primary_issue not in ISSUE_TOOLS:
        primary_issue = "unsupported_claim"
    return {
        "case_id": case_id,
        "order_id": order_id,
        "policy_version": _required_string(case, "policy_version"),
        "claims": normalized_claims,
        "primary_issue": primary_issue,
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    plan = coordinate_case(case, trace)
    case_id = plan["case_id"]
    order_id = plan["order_id"]
    primary_issue = plan["primary_issue"]
    evidence_by_tool: dict[str, dict[str, Any]] = {}

    for actor, tool_name in ISSUE_TOOLS[primary_issue]:
        evidence = await gateway.call(tool_name, case_id=case_id, order_id=order_id)
        evidence_by_tool[tool_name] = evidence
        _emit_consumed(trace, case_id, actor, tool_name, evidence)

    policy = await gateway.call(
        "get_policy", case_id=case_id, policy_version=plan["policy_version"]
    )
    evidence_by_tool["get_policy"] = policy
    _emit_consumed(trace, case_id, "policy-agent", "get_policy", policy)
    policy_data = policy.get("data")
    rules = policy_data.get("rules") if isinstance(policy_data, dict) else None
    rule = rules.get(primary_issue) if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        raise ValueError(f"policy has no rule for {primary_issue}")

    policy_ref = _evidence_ref(policy)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=primary_issue.upper(),
        evidence_refs=[policy_ref],
    )
    specialist_refs = [
        _evidence_ref(evidence)
        for tool, evidence in evidence_by_tool.items()
        if tool != "get_policy"
    ]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="SPECIALIST_EVIDENCE_READY",
        evidence_refs=[*specialist_refs, policy_ref],
        attributes={"primary_issue": primary_issue},
    )

    output = _build_output(plan, evidence_by_tool, rule)
    _verify_output(output)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="OUTPUT_INVARIANTS_PASSED",
        evidence_refs=output["evidence_refs"],
    )
    return output


def _build_output(
    plan: dict[str, Any],
    evidence_by_tool: dict[str, dict[str, Any]],
    rule: dict[str, Any],
) -> dict[str, Any]:
    issue = plan["primary_issue"]
    all_refs = [_evidence_ref(value) for value in evidence_by_tool.values()]
    entities = _extract_entities(plan["order_id"], evidence_by_tool.values())
    refund = _money(rule.get("refund_brl"))
    action = str(rule.get("recommended_action", "document_no_action"))
    case_status = str(rule.get("case_status", "needs_investigation"))
    responsible = _responsible_parties(rule.get("responsible_parties"))

    claim_assessments = []
    for claim in plan["claims"]:
        topic = claim["topic"]
        if topic == issue:
            verdict, confidence = "supported", 0.99
        elif topic == "requested_full_refund" and refund > 0:
            verdict, confidence = "supported", 0.98
        elif topic == "requested_full_refund" and case_status == "needs_investigation":
            verdict, confidence = "insufficient_evidence", 0.75
        else:
            verdict, confidence = "unsupported", 0.98
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": all_refs,
            }
        )

    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": action.upper(),
                "amount_brl": refund,
                "entity_id": plan["order_id"],
            }
        )
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": plan["case_id"],
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": 0.99,
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": all_refs,
        "data_conflicts": _find_conflicts(evidence_by_tool),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


def _extract_entities(
    claimed_order_id: str, evidence_values: Any
) -> dict[str, list[str]]:
    keys = {
        "order_id": "order_ids",
        "order_item_id": "item_ids",
        "item_id": "item_ids",
        "seller_id": "seller_ids",
        "payment_reference": "payment_references",
        "payment_id": "payment_references",
        "shipment_id": "shipment_ids",
    }
    found: dict[str, set[str]] = {
        "order_ids": {claimed_order_id},
        "item_ids": set(),
        "seller_ids": set(),
        "payment_references": set(),
        "shipment_ids": set(),
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                bucket = keys.get(key)
                if bucket is not None and isinstance(child, (str, int)):
                    found[bucket].add(str(child))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for evidence in evidence_values:
        visit(evidence.get("data"))
    return {key: sorted(values)[:20] for key, values in found.items()}


def _find_conflicts(
    evidence_by_tool: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    identity_keys = ("order_item_id", "payment_sequential", "shipment_id")
    for tool_name, evidence in evidence_by_tool.items():
        data = evidence.get("data")
        if isinstance(data, list):
            _append_row_conflicts(conflicts, data, tool_name, identity_keys)
        elif isinstance(data, dict):
            for candidate in ("payments", "events", "shipping_limits"):
                rows = data.get(candidate)
                if isinstance(rows, list):
                    _append_row_conflicts(
                        conflicts, rows, f"{tool_name}.{candidate}", identity_keys
                    )
        if len(conflicts) >= 5:
            break
    return conflicts[:5]


def _append_row_conflicts(
    conflicts: list[dict[str, Any]],
    rows: list[Any],
    source: str,
    identity_keys: tuple[str, ...],
) -> None:
    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        identity = next(
            ((key, str(row[key])) for key in identity_keys if row.get(key) is not None),
            None,
        )
        if identity is not None:
            groups[identity].append((index, row))
    for (identity_key, identity), grouped in groups.items():
        if len(grouped) < 2:
            continue
        fields = set().union(*(row.keys() for _, row in grouped))
        for field in sorted(fields - {"order_id", identity_key}):
            if len({str(row.get(field)) for _, row in grouped}) < 2:
                continue
            sources = [f"{source}[{index}]" for index, _ in grouped[:5]]
            conflicts.append(
                {
                    "field": f"{identity_key}={identity}.{field}"[:100],
                    "sources": sources,
                    "selected_source": sources[0],
                    "resolution_code": "AUTHORITATIVE_TIMELINE_PRECEDENCE",
                }
            )
            if len(conflicts) >= 5:
                return


def _responsible_parties(value: Any) -> list[dict[str, Any]]:
    allowed = {
        "seller", "platform", "logistics_provider", "payment_provider",
        "customer", "unknown",
    }
    result = []
    if isinstance(value, list):
        for party in value[:5]:
            if not isinstance(party, dict):
                continue
            party_type = party.get("party_type")
            if party_type not in allowed:
                party_type = "unknown"
            party_id = party.get("party_id")
            result.append(
                {
                    "party_type": party_type,
                    "party_id": party_id if isinstance(party_id, str) else None,
                }
            )
    return result or [{"party_type": "unknown", "party_id": None}]


def _verify_output(output: dict[str, Any]) -> None:
    financial = output["financial_resolution"]
    line_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if line_total != round(financial["recommended_refund_brl"], 2):
        raise ValueError("refund lines do not equal the recommended refund")
    if (
        output["assessment"]["case_status"] == "no_action"
        and financial["recommended_refund_brl"] != 0
    ):
        raise ValueError("no_action output cannot recommend a refund")
    known_refs = set(output["evidence_refs"])
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]).issubset(known_refs):
            raise ValueError("claim cites evidence outside the case evidence set")


def _emit_consumed(
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool_name: str,
    evidence: dict[str, Any],
) -> None:
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[_evidence_ref(evidence)],
    )


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"missing or invalid {key}")
    return result.strip()


def _evidence_ref(evidence: dict[str, Any]) -> str:
    return _required_string(evidence, "evidence_ref")


def _money(value: Any) -> float:
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid policy refund amount: {value!r}") from exc
