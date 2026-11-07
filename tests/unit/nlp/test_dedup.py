"""去重单测：哈希、5-gram Jaccard、近重复保护（实现文档 §4.4）。"""

from app.nlp.dedup import (
    file_sha256,
    five_gram_jaccard,
    is_protected_difference,
    near_duplicate_pairs,
    text_sha256,
)


def test_file_sha256_stable():
    assert file_sha256(b"abc") == file_sha256(b"abc")
    assert file_sha256(b"abc") != file_sha256(b"abd")


def test_text_sha256_is_bytes():
    digest = text_sha256("员工报销")
    assert isinstance(digest, bytes)
    assert len(digest) == 32


def test_jaccard_bounds():
    assert five_gram_jaccard("员工应在30日内申请报销", "员工应在30日内申请报销") == 1.0
    assert five_gram_jaccard("完全不同的话题", "毫无交集的内容") < 0.1


def test_near_duplicate_detected():
    # 无重复长句、仅末字符差异：gram 集合几乎不变，Jaccard ≈ 0.98 ≥ 0.9
    a = (
        "员工在出差结束后应当于三十日内提交报销申请，并且必须附上有效票据以及部门负责人签字确认的审批单据，"
        "同时需要在办公系统中完成电子化流程的填报与归档操作，财务部门将在收到完整材料后的五个工作日内完成审核，"
        "审核通过后报销款项将直接发放至员工工资卡账户，如有疑问可以随时联系财务部门的热线电话进行咨询与确认"
    )
    b = a[:-1] + "备"
    assert (0, 1) in near_duplicate_pairs([(0, a), (1, b)])
    # 短文本差异大时不构成近重复
    assert near_duplicate_pairs([(0, "员工报销流程"), (1, "考勤管理规定")]) == []


def test_protected_difference_excluded():
    base = "员工应在出差结束后30日内提交报销申请，标准额度为500元，须附上有效票据与审批单据"
    changed = "员工应在出差结束后30日内提交报销申请，标准额度为600元，须附上有效票据与审批单据"
    pairs = near_duplicate_pairs([(0, base), (1, changed)])
    assert pairs == []  # 金额变化受保护，不得按近重复丢弃
    assert is_protected_difference(base, changed)


def test_negation_change_protected():
    a = "本条款适用于全部员工，包括试用期员工在内"
    b = "本条款不适用于全部员工，包括试用期员工在内"
    assert is_protected_difference(a, b)
