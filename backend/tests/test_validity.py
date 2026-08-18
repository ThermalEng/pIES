"""结果四维有效性模型单元测试(01 §8.2 与核心不变量 4)。

纯计算测试,不依赖 DB。
"""

import pytest

from iesplan.metrics.financial import IRRStatus
from iesplan.metrics.validity import (
    FinancialValidity,
    OptimalityValidity,
    PhysicalValidity,
    ReliabilityStatus,
    ValidityLevel,
    financial_validity_from_irr,
    from_db_value,
    summarize_four_dimensions,
)

PASSED = PhysicalValidity.passed
OK = ReliabilityStatus.ok


class TestSummarizeFourDimensions:
    """汇总只派生 可用/受限使用/不可用,且不隐藏任一维度。"""

    def test_all_passed_usable(self):
        r = summarize_four_dimensions(
            PhysicalValidity.passed,
            OptimalityValidity.passed,
            FinancialValidity.passed,
            ReliabilityStatus.ok,
            financial_irr_status=IRRStatus.unique,
        )
        assert r["summary"] == "usable"
        # 核心不变量 4:四个维度全部可见
        assert r["dimensions"] == {
            "physical": "passed",
            "optimality": "passed",
            "financial": "passed",
            "financial_irr_status": "unique",
            "reliability": "ok",
        }

    def test_physical_failed_unusable(self):
        r = summarize_four_dimensions(
            PhysicalValidity.failed,
            OptimalityValidity.passed,
            FinancialValidity.passed,
            ReliabilityStatus.ok,
        )
        assert r["summary"] == "unusable"
        assert "physical:failed" in r["reasons"]

    def test_any_failed_unusable(self):
        r = summarize_four_dimensions(
            PhysicalValidity.passed,
            OptimalityValidity.failed,
            FinancialValidity.passed,
            ReliabilityStatus.ok,
        )
        assert r["summary"] == "unusable"

    def test_financial_restricted(self):
        # IRR 多根 -> 财务受限 -> 受限使用
        r = summarize_four_dimensions(
            PhysicalValidity.passed,
            OptimalityValidity.passed,
            FinancialValidity.restricted,
            ReliabilityStatus.ok,
            financial_irr_status=IRRStatus.multiple,
        )
        assert r["summary"] == "restricted"
        assert "financial:restricted" in r["reasons"]
        assert "irr_status:multiple" in r["reasons"]

    def test_reliability_partial_restricted(self):
        r = summarize_four_dimensions(
            PASSED,
            PASSED,
            PASSED,
            ReliabilityStatus.partial,
        )
        assert r["summary"] == "restricted"
        assert "reliability:partial" in r["reasons"]

    def test_reliability_insufficient_restricted(self):
        r = summarize_four_dimensions(
            PASSED,
            PASSED,
            PASSED,
            ReliabilityStatus.insufficient,
        )
        assert r["summary"] == "restricted"

    def test_insufficient_evidence_restricted(self):
        r = summarize_four_dimensions(
            PASSED,
            PASSED,
            FinancialValidity.insufficient,
            OK,
        )
        assert r["summary"] == "restricted"

    def test_not_executed_not_hidden(self):
        # 可靠性未执行:不参与判定但必须可见
        r = summarize_four_dimensions(PASSED, PASSED, PASSED, ReliabilityStatus.not_executed)
        assert r["summary"] == "usable"
        assert r["dimensions"]["reliability"] == "not_executed"
        assert "reliability:not_executed" in r["reasons"]

    def test_all_na_unusable(self):
        r = summarize_four_dimensions(
            ValidityLevel.na,
            ValidityLevel.na,
            ValidityLevel.na,
            ReliabilityStatus.not_executed,
        )
        assert r["summary"] == "unusable"

    def test_string_inputs_accepted(self):
        r = summarize_four_dimensions("passed", "passed", "passed", "ok")
        assert r["summary"] == "usable"

    def test_output_metadata(self):
        r = summarize_four_dimensions(PASSED, PASSED, PASSED, OK)
        assert r["definition_version"] == "1.0.0"
        assert r["refs"]

    def test_invalid_input_rejected(self):
        with pytest.raises(ValueError):
            summarize_four_dimensions("bogus", PASSED, PASSED, OK)


class TestFinancialValidityFromIrr:
    """IRR 状态 -> 财务有效性细分(REQ-FIN-005)。"""

    def test_unique_passed(self):
        assert financial_validity_from_irr(IRRStatus.unique) == FinancialValidity.passed

    def test_multiple_restricted(self):
        assert financial_validity_from_irr(IRRStatus.multiple) == FinancialValidity.restricted

    def test_none_failed(self):
        assert financial_validity_from_irr(IRRStatus.none) == FinancialValidity.failed

    def test_out_of_domain_failed(self):
        assert financial_validity_from_irr(IRRStatus.out_of_domain) == FinancialValidity.failed

    def test_degenerate_na(self):
        assert financial_validity_from_irr(IRRStatus.degenerate) == FinancialValidity.na

    def test_numerical_failure_restricted(self):
        assert financial_validity_from_irr(IRRStatus.numerical_failure) == FinancialValidity.restricted

    def test_none_irr_insufficient(self):
        assert financial_validity_from_irr(None) == FinancialValidity.insufficient


class TestFromDbValue:
    """DB 枚举(pass/fail/unknown)到内存细分状态的映射。"""

    def test_pass(self):
        assert from_db_value("pass", "physical") == ValidityLevel.passed

    def test_fail(self):
        assert from_db_value("fail", "financial") == ValidityLevel.failed

    def test_unknown_na(self):
        assert from_db_value("unknown", "optimality") == ValidityLevel.na

    def test_reliability(self):
        assert from_db_value("pass", "reliability") == ReliabilityStatus.ok
        assert from_db_value("fail", "reliability") == ReliabilityStatus.insufficient
        assert from_db_value("unknown", "reliability") == ReliabilityStatus.not_executed
        assert from_db_value(None, "reliability") == ReliabilityStatus.not_executed

    def test_none_na(self):
        assert from_db_value(None, "physical") == ValidityLevel.na
