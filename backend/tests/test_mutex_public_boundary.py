"""The shared mutex lowering is public and retains finite-bound semantics."""

from iesplan.computation.generators import lower_mutex


def test_public_mutex_lowering_uses_each_actual_finite_bound() -> None:
    lowered = lower_mutex(
        "tax_net", ("paid", 0), ("carry", 0),
        {"paid": ("var", 0.0, 12.5), "carry": ("var", 0.0, 37.0)},
    )
    (binary,) = lowered.variables
    assert (binary.name, binary.lower, binary.upper, binary.kind) == (
        "tax_net__mutex", 0.0, 1.0, "binary",
    )
    rows = {row.name: row for row in lowered.constraints}
    assert dict(rows["tax_net__mutex_x"].expression.terms) == {
        ("paid", 0): 1.0, (binary.name, 0): -12.5,
    }
    assert (rows["tax_net__mutex_x"].sense, rows["tax_net__mutex_x"].rhs) == ("le", 0.0)
    assert dict(rows["tax_net__mutex_y"].expression.terms) == {
        ("carry", 0): 1.0, (binary.name, 0): 37.0,
    }
    assert (rows["tax_net__mutex_y"].sense, rows["tax_net__mutex_y"].rhs) == ("le", 37.0)
