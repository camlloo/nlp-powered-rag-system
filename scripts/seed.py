"""初始化种子数据：确保主空间存在（幂等，可重复执行）。

去知识库化后系统只有单一「主空间」（组织/个人归属由文档级字段承载）；
演示文档可直接在后台「文档管理」上传（归属组织并设为公开）。
"""

import sys

from app.db.session import SessionLocal
from app.db.spaces import get_or_create_main_space


def main() -> int:
    with SessionLocal() as session:
        kb = get_or_create_main_space(session)
        print(
            f"主空间就绪 kb_id={kb.id} name={kb.name!r} "
            f"data_revision={kb.data_revision}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
