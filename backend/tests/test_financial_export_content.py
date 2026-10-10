"""财务摘要下载与封存事实的精确对照(测试断言, 非运行期校验)。

阳性: 检入的真实下载 xlsx(财务摘要表)与同一封存 JSON 逐行
(标签、值、顺序、行数)精确一致, 覆盖 K/N/E/C/币种、付款时点
比例金额(同一 t 多笔按序)、每年度全部分量、CF 每个时点净额/
贴现/累计/贴现累计、NPV 与 IRR/回收期值和状态。

阴性对照(不入生产): 内存中交换两 CF 行数值 / 错改 K(相同数字
仍在别处存在), 精确对照必须失败——旧全表子串命中对此类篡改
仍为绿, 见提交说明中的对照证据。
"""

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook
from xlsx_content_check import assert_block_exact, extract_pairs

FIXTURES = Path(__file__).resolve().parent / "fixtures_t3_export"


def _load_fixtures():
    sealed = json.loads((FIXTURES / "t3-sealed.json").read_text(encoding="utf-8"))
    wb = load_workbook(FIXTURES / "t3-report.xlsx", read_only=True, data_only=True)
    return sealed, extract_pairs(wb["财务摘要"])


def test_downloaded_xlsx_matches_sealed_facts_exactly():
    sealed, pairs = _load_fixtures()
    assert_block_exact(pairs, sealed)


def test_negative_swapped_cf_nets_must_fail():
    """交换 CF[t=0]/CF[t=1] 净额: 数字仍在表内, 精确对照必须失败。"""
    sealed, pairs = _load_fixtures()
    net_rows = [i for i, (label, _) in enumerate(pairs) if label.endswith("净额 / Net")]
    assert len(net_rows) >= 2
    a, b = net_rows[0], net_rows[1]
    pairs[a], pairs[b] = (pairs[a][0], pairs[b][1]), (pairs[b][0], pairs[a][1])
    with pytest.raises(AssertionError, match="值不符"):
        assert_block_exact(pairs, sealed)


def test_negative_wrong_k_must_fail():
    """错改 K=1→2(数字 2 在 N/年度行仍存在): 精确对照必须失败。"""
    sealed, pairs = _load_fixtures()
    idx = next(
        i for i, (label, _) in enumerate(pairs) if label.startswith("建设期 K")
    )
    pairs[idx] = (pairs[idx][0], 2)
    with pytest.raises(AssertionError, match="值不符"):
        assert_block_exact(pairs, sealed)
