"""Small D2 input/projection checks, not an automatic annual benchmark."""
import json

import pytest
from stage_d_tax_diagnosis import tax_inputs
from test_issue06_s2 import _builder
from test_p3_f2_execution import _plain, _relations
from test_stage_d_diagnosis import solve, sources


def test_tax_control_changes_only_explicit_financial_rules_not_technical_inputs():
    full, none = tax_inputs("full_tax"), tax_inputs("no_tax")
    assert json.loads(full[0].canonical_text) == json.loads(none[0].canonical_text)
    assert full[1:3] == none[1:3]
    assert full[4:] == none[4:]
    a, b = full[3].to_dict(), none[3].to_dict()
    for key in ("taxes", "tax_rules", "credit_pools"):
        assert a.pop(key)
        assert b.pop(key) == {}
    assert a == b
    assert full[5].operation_years == 2
    assert full[4].time_limit_seconds == 30 and full[4].seed == 0


def test_opening_loss_control_preserves_all_other_fields_and_cross_period_identity():
    full, loss = tax_inputs("full_tax"), tax_inputs("opening_loss")
    assert full[0].canonical_text == loss[0].canonical_text
    assert full[1:3] == loss[1:3]
    assert full[4:] == loss[4:]
    a, b = full[3].to_dict(), loss[3].to_dict()
    assert a["tax_rules"]["income"].pop("opening_losses") == []
    assert b["tax_rules"]["income"].pop("opening_losses") == [
        {"amount": {"value": "450", "unit": "CNY"}, "last_usable_year": 2},
    ]
    assert a == b


@pytest.mark.parametrize("mode", ["exact", "relaxation"])
def test_financial_projection_uses_the_same_native_primal_and_keeps_mode_label(tmp_path, mode):
    builder = _builder([], fixed_capacity=True, finance_patch=_relations)
    model, _, _ = builder.build()
    result = solve(model, tmp_path, mode, sources(), financial_declarations=_plain(builder.financial_facts()))
    facts = result["financial_facts_of_experiment"]
    assert facts is not None
    assert result["is_original_milp_incumbent"] == (mode == "exact")
    assert [float(x) for x in facts["annual_net_flows"]] == pytest.approx([-44, 165.1, 154.8])
    assert [float(row["tax"]) for row in facts["tax_cash"]["income"]] == pytest.approx([44.9, 44.8])
    vat = {row["month"]: row for row in facts["tax_cash"]["vat"]}
    assert [float(vat[m]["paid"]) for m in (12, 24)] == pytest.approx([10, 20])
