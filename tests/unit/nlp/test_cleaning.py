"""清洗模块单测：PDF 抽取噪声折叠（点线引导符与连续空白）。"""

from app.nlp.cleaning import fold_pdf_noise


def test_fold_dot_leaders():
    assert fold_pdf_noise("1. HTML 概述  .  .  .  .  .  .  .  6") == "1. HTML 概述 6"
    assert fold_pdf_noise("Chapter 1 Overview . . . . . . . . 6") == "Chapter 1 Overview 6"


def test_fold_keeps_ellipsis_and_numbering():
    assert fold_pdf_noise("等待中...") == "等待中..."
    assert fold_pdf_noise("1. 第一项") == "1. 第一项"
    assert fold_pdf_noise("版本 1.2.3 发布") == "版本 1.2.3 发布"


def test_fold_collapses_space_runs_keeps_newlines():
    assert fold_pdf_noise("甲  乙\t\t丙   丁") == "甲 乙 丙 丁"
    assert fold_pdf_noise("标题\n\n  正文 内容") == "标题\n\n 正文 内容"


def test_fold_removes_leader_only_line_prefix():
    # 目录项换行后整行以点线开头：点线整体消失，仅保留页码
    assert fold_pdf_noise(" .  .  .  .  .  .  6") == " 6"
