"""Explicit D2 public tax controls, reusing the P3 N/F integrated fixture."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from stage_d_diagnosis import diagnose
from test_p3_f3_nf_integration import _case
from test_p3_port_network import _inputs

from iesplan.assembly import validate_assembly_doc
from iesplan.devices import init_registry
from iesplan.finance import EffectiveFinanceConfig

CASES = ("no_tax", "full_tax", "opening_loss")


def tax_inputs(case: str):
    """Each variant is a declared DIFFERENT input, not an exact reformulation."""
    init_registry()
    inputs = list(_case(0.8, "110"))
    raw = inputs[3].to_dict()
    if case == "no_tax":
        raw.update(taxes={}, credit_pools={}, tax_rules={})
    elif case == "opening_loss":
        raw["tax_rules"]["income"]["opening_losses"] = [
            {"amount": {"value": "450", "unit": "CNY"}, "last_usable_year": 2},
        ]
    elif case != "full_tax":
        raise ValueError(case)
    finance = EffectiveFinanceConfig.from_dict(raw)
    doc = json.loads(inputs[0].canonical_text)
    # Public fixture contributions are already immutable, so revalidate the
    # financial binding against the changed public snapshot, not an old receipt.
    _, documents = _inputs()
    checked = validate_assembly_doc(doc, device_documents=documents,
                                    finance_snapshots={"obj-finance": finance})
    assert checked.ok, checked.diagnostics
    inputs[0], inputs[3] = checked.artifact, finance
    inputs[4] = replace(inputs[4], time_limit_seconds=30, seed=0)
    return tuple(inputs)


def diagnose_tax(case: str, mode: str, seconds: int, log_path: Path) -> dict:
    out = diagnose(2, 1, mode, seconds, log_path,
                   inputs_factory=lambda *_: tax_inputs(case),
                   fixture=f"public P3 N/F two-year integration: {case}")
    del out["generation"]["whole_period_purchase_budget_factor"]
    out["coverage"]["input_comparison"] = (
        "explicit tax-rule or opening-loss input changes; NOT equivalent optimization"
    )
    out["coverage"]["tax_case"] = case
    out["coverage"]["tax_performance_conclusion"] = (
        "only this public fixture; not the original engineering model"
    )
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--mode", choices=("exact", "relaxation"), required=True)
    parser.add_argument("--solve-seconds", type=int, choices=range(1, 31), default=30)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose_tax(args.case, args.mode, args.solve_seconds,
                          args.report.with_suffix(".solver.log"))
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    args.report.write_text(content + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.report), "case": args.case, "mode": args.mode,
                      "status": result["native"]["model_status"],
                      "generate_seconds": result["generation"]["generate_seconds"],
                      "solve_seconds": result["native"]["run_seconds"]}), flush=True)
