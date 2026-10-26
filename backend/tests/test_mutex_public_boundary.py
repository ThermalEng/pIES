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


def test_mutex_factor_exchange_is_binary_complement_not_a_flow_restriction():
    """Swapping factors changes only the internal bit's polarity."""
    symbols = {"x": ("var", 0.0, 3.0), "y": ("var", 0.0, 5.0)}
    forward = lower_mutex("flow", ("x", 0), ("y", 0), symbols)
    reverse = lower_mutex("flow", ("y", 0), ("x", 0), symbols)
    bit = ("flow__mutex", 0)

    def feasible(lowered, x, y, b):
        values = {("x", 0): x, ("y", 0): y, bit: b}
        return all(
            row.expression.constant + sum(c * values[k] for k, c in row.expression.terms) <= row.rhs
            for row in lowered.constraints
        )

    for x in range(4):
        for y in range(6):
            for b in (0, 1):
                assert feasible(forward, x, y, b) == feasible(reverse, x, y, 1-b)
            assert any(feasible(forward, x, y, b) for b in (0, 1)) == (x*y == 0)
