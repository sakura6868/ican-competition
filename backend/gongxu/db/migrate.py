"""统一数据库启动入口，兼容空库、正常升级库与旧版 create_all 数据库。"""

from __future__ import annotations

from gongxu.db.session import init_db


def main() -> int:
    init_db()
    print("database migration ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
