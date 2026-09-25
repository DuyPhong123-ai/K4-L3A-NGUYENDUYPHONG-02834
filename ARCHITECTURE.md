# L3A Architecture Record

## 1. System overview

```text
Input -> Coordinator -> Domain specialists -> Policy -> Verifier -> Output
                            |                    |
                            +------ MCP ---------+---- Trace
```

The coordinator validates case identity, extracts the claimed order and routes every
claim. Specialists obtain authoritative, case-scoped MCP evidence. The policy agent
maps the verified primary issue to the machine-readable competition policy. The
verifier checks financial and evidence invariants before returning the output.

## 2. Agent ownership

| Actor | MCP tools | Responsibility |
| --- | --- | --- |
| Coordinator | tool discovery only | Claim routing, correlation and handoff |
| Order/item | `get_order`, `get_order_items`, `get_sellers` | Order state and affected commercial entities |
| Payment | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Charges, payment totals and refund lifecycle |
| Shipment | `get_shipment_summary` | Delivery timeline and late-delivery actor |
| Policy | `get_policy` | Status, action, refund and responsible-party rule |
| Verifier | none | Schema-facing consistency and evidence linkage |

Tools are selected by issue so the output contains only evidence relevant to the case.

## 3. A2A protocol

Messages are represented by observable trace events correlated by `case_id`.
`task_assigned` records claim routing, `tool_result_consumed` links each specialist to
the evidence it used, `policy_decided` records the applied rule, and `handoff` transfers
the evidence bundle to the verifier. One bounded coordinator-to-verifier handoff avoids
cycles. MCP transport timeouts are defined centrally by the gateway.

## 4. Evidence lifecycle

Every MCP response is validated against `mcp-evidence-response-v1` before use. Evidence
references are copied verbatim, kept within their originating case, attached to claim
assessments and linked in trace events. Entity identifiers are recursively extracted
from authoritative response data. The implementation never synthesizes evidence refs.

## 5. Failure policy

| Failure | Retry | Fallback | Result |
| --- | --- | --- | --- |
| MCP timeout/transport failure | External rerun only | None | Case run fails rather than inventing evidence |
| Required evidence not found | No | None | Case run fails |
| Source conflict | No | Record bounded `data_conflicts` | Authoritative timeline/policy precedence |
| Invalid policy or specialist response | No | None | Contract or value error |

## 6. Verification invariants

- Refund line totals equal `recommended_refund_brl`.
- `no_action` never carries a positive refund.
- Every claim evidence ref belongs to the case-level evidence set.
- Evidence and trace refs are server-issued and schema-valid.
- Entity lists are unique, sorted and contract-bounded.
- Confidence values and all enum-like fields remain inside the public schema.

## 7. Reproducibility

The workflow is deterministic for a fixed input and MCP snapshot. Dependencies are
bounded in `pyproject.toml`; there is no model randomness or hidden prompt. Run with
`day09 run`, validate with `day09 validate`, and package with `day09 package`. Secrets
are read only from `.env` and are never written to artifacts or trace.
