"""表达式引擎单元测试:正确求值、白名单安全、恶意输入拒绝。"""

import pytest

from iesplan.core.expression import (
    ExpressionCodeError,
    ExpressionDimensionError,
    ExpressionError,
    ExpressionRangeError,
    ExpressionRunError,
    ExpressionSecurityError,
    ExpressionSyntaxError,
    ExpressionTypeError,
    _dim_of,
    parse_expr,
)

VARS = {"a", "b", "x", "y", "theta"}


class TestEval:
    """正常求值。"""

    def test_arithmetic(self):
        expr = parse_expr("a * 2 + b / 3", VARS)
        assert expr.eval({"a": 3, "b": 9}) == pytest.approx(6.0 + 3.0)

    def test_unary_neg(self):
        expr = parse_expr("-a + b", VARS)
        assert expr.eval({"a": 1, "b": 2}) == pytest.approx(1.0)

    def test_pow(self):
        expr = parse_expr("a ^ 2", VARS)
        assert expr.eval({"a": 5}) == pytest.approx(25.0)
        assert parse_expr("2 ** 10", VARS).eval({}) == pytest.approx(1024.0)

    def test_functions(self):
        assert parse_expr("abs(a)", VARS).eval({"a": -3.5}) == pytest.approx(3.5)
        assert parse_expr("min(a, b, x)", VARS).eval({"a": 3, "b": 1, "x": 2}) == pytest.approx(1.0)
        assert parse_expr("max(a, b)", VARS).eval({"a": 3, "b": 1}) == pytest.approx(3.0)
        # sin/cos 参数为角度(deg)
        assert parse_expr("sin(90)", VARS).eval({}) == pytest.approx(1.0)
        assert parse_expr("cos(0)", VARS).eval({}) == pytest.approx(1.0)
        assert parse_expr("tan(45)", VARS).eval({}) == pytest.approx(1.0)
        assert parse_expr("sqrt(16)", VARS).eval({}) == pytest.approx(4.0)
        assert parse_expr("exp(0)", VARS).eval({}) == pytest.approx(1.0)
        assert parse_expr("log(2.718281828459045)", VARS).eval({}) == pytest.approx(1.0)
        assert parse_expr("pow(2, 8)", VARS).eval({}) == pytest.approx(256.0)

    def test_comparison_and_logic(self):
        assert parse_expr("a > b", VARS).eval({"a": 2, "b": 1}) == 1.0
        assert parse_expr("a > b", VARS).eval({"a": 1, "b": 2}) == 0.0
        assert parse_expr("a >= 2 and b < 5", VARS).eval({"a": 2, "b": 3}) == 1.0
        assert parse_expr("a >= 2 or b < 5", VARS).eval({"a": 0, "b": 9}) == 0.0
        assert parse_expr("a != b", VARS).eval({"a": 1, "b": 2}) == 1.0

    def test_if_function(self):
        expr = parse_expr("if(x > 10, x * 0.5, x)", VARS)
        assert expr.eval({"x": 20}) == pytest.approx(10.0)
        assert expr.eval({"x": 5}) == pytest.approx(5.0)

    def test_nested_parens(self):
        expr = parse_expr("(a + b) * (a - b)", VARS)
        assert expr.eval({"a": 5, "b": 2}) == pytest.approx(21.0)

    def test_compiled_metadata(self):
        expr = parse_expr("a + b * 2", VARS)
        assert expr.text == "a + b * 2"
        assert expr.allowed_vars == frozenset(VARS)
        assert 0 < expr.node_count <= 512


class TestDimChecks:
    """量纲一致性(04 §4.2 步骤 3)。"""

    def test_add_mismatch_rejected(self):
        dims = {"a": _dim_of({"power": 1}), "b": _dim_of({"energy": 1})}
        with pytest.raises(ExpressionDimensionError) as ei:
            parse_expr("a + b", VARS, var_dims=dims)
        assert ei.value.code == "EXPR-DIM-001"

    def test_mul_combines_dims(self):
        dims = {"a": _dim_of({"power": 1}), "b": _dim_of({"time": 1})}
        # power * time = energy,与 energy 相加合法
        expr = parse_expr("a * b", VARS, var_dims=dims)
        assert expr.eval({"a": 2, "b": 3}) == pytest.approx(6.0)

    def test_sqrt_even_dims(self):
        dims = {"a": _dim_of({"energy": 2})}
        parse_expr("sqrt(a)", VARS, var_dims=dims)  # 不抛异常
        dims_bad = {"a": _dim_of({"energy": 1})}
        with pytest.raises(ExpressionDimensionError):
            parse_expr("sqrt(a)", VARS, var_dims=dims_bad)

    def test_sin_requires_dimensionless(self):
        dims = {"theta": _dim_of({"angle": 1})}
        with pytest.raises(ExpressionDimensionError):
            parse_expr("sin(theta)", VARS, var_dims=dims)

    def test_compare_mismatch_rejected(self):
        dims = {"a": _dim_of({"power": 1}), "b": _dim_of({"energy": 1})}
        with pytest.raises(ExpressionDimensionError):
            parse_expr("a > b", VARS, var_dims=dims)


class TestSecurity:
    """恶意输入必须被拒绝(04 §4.3 禁止列表)。"""

    def test_unknown_function_rejected(self):
        for text in ["evil(1)", "os.system('ls')", "getattr(a, '__class__')"]:
            with pytest.raises(ExpressionSecurityError):
                parse_expr(text, VARS)

    def test_attribute_access_rejected(self):
        with pytest.raises(ExpressionSecurityError):
            parse_expr("a.__class__", VARS)

    def test_import_pattern_rejected(self):
        with pytest.raises(ExpressionSecurityError):
            parse_expr("import os", VARS)
        with pytest.raises(ExpressionSecurityError):
            parse_expr("__import__('os')", VARS)
        # 混淆手段(04 §4.3:任何含 import/eval 字样的标识符一律拒绝)
        with pytest.raises(ExpressionSecurityError):
            parse_expr("a_import_x(1)", VARS)

    def test_eval_exec_rejected(self):
        with pytest.raises(ExpressionSecurityError):
            parse_expr("eval('1+1')", VARS)
        with pytest.raises(ExpressionSecurityError):
            parse_expr("exec('x=1')", VARS)

    def test_string_literal_rejected(self):
        with pytest.raises(ExpressionSecurityError):
            parse_expr("'hello'", VARS)
        with pytest.raises(ExpressionSecurityError):
            parse_expr("a + 'x'", VARS)

    def test_lambda_and_comprehension_rejected(self):
        with pytest.raises(ExpressionSecurityError):
            parse_expr("lambda x: x", VARS)
        with pytest.raises(ExpressionSecurityError):
            parse_expr("[a for a in [1,2]]", VARS)

    def test_list_dict_literals_rejected(self):
        with pytest.raises(ExpressionSecurityError):
            parse_expr("[1, 2, 3]", VARS)
        with pytest.raises(ExpressionSecurityError):
            parse_expr("{'k': 1}", VARS)

    def test_subscript_rejected(self):
        with pytest.raises(ExpressionSecurityError):
            parse_expr("a[0]", VARS)

    def test_unknown_variable_rejected(self):
        with pytest.raises(ExpressionCodeError) as ei:
            parse_expr("zzz + 1", VARS)
        assert ei.value.code == "EXPR-CODE-001"

    def test_syntax_error(self):
        with pytest.raises(ExpressionSyntaxError):
            parse_expr("a +", VARS)
        with pytest.raises(ExpressionSyntaxError):
            parse_expr("", VARS)

    def test_node_limit(self):
        # 远超 512 节点的表达式被拒(EXPR-RNG-001)
        long_expr = " + ".join(["1"] * 2000)
        with pytest.raises(ExpressionRangeError):
            parse_expr(long_expr, VARS)

    def test_pow_non_constant_exponent(self):
        with pytest.raises(ExpressionTypeError):
            parse_expr("a ^ b", VARS)

    def test_no_runtime_code_execution(self):
        """eval 阶段不执行任何 Python 对象访问;恶意输入在 parse 阶段即被拦截。"""
        for text in ["__builtins__", "().__class__", "open('/etc/passwd')"]:
            with pytest.raises(ExpressionError):
                parse_expr(text, VARS)


class TestRuntimeTraps:
    """运行期数值陷阱(04 §4.4:除零/定义域/溢出拒绝)。"""

    def test_division_by_zero(self):
        expr = parse_expr("a / b", VARS)
        with pytest.raises(ExpressionRunError):
            expr.eval({"a": 1, "b": 0})

    def test_log_domain(self):
        expr = parse_expr("log(a)", VARS)
        with pytest.raises(ExpressionRunError):
            expr.eval({"a": -1})

    def test_sqrt_negative(self):
        expr = parse_expr("sqrt(a)", VARS)
        with pytest.raises(ExpressionRunError):
            expr.eval({"a": -4})

    def test_missing_variable_at_eval(self):
        expr = parse_expr("a + 1", VARS)
        with pytest.raises(ExpressionCodeError):
            expr.eval({})  # 未提供 a

    def test_overflow_rejected(self):
        # exp(1000) 编译通过(无范围限制),求值溢出被拒
        expr = parse_expr("exp(1000)", VARS)
        with pytest.raises(ExpressionRunError):
            expr.eval({})
        # 幂指数超出范围在编译期拒绝(EXPR-RNG-001)
        with pytest.raises(ExpressionRangeError):
            parse_expr("pow(10, 400)", VARS)

    def test_non_numeric_variable(self):
        expr = parse_expr("a + 1", VARS)
        with pytest.raises(ExpressionRunError):
            expr.eval({"a": "x"})
