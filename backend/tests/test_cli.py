"""CLI 测试。"""

from __future__ import annotations

from gongxu.cli import cmd_create_admin


def test_create_admin_cli(test_db) -> None:
    class Args:
        username = "cli-admin"
        password = "cli-pass-12345"
        display_name = "CLI 管理员"

    code = cmd_create_admin(Args())
    assert code == 0


def test_create_admin_duplicate_rejected(test_db) -> None:
    class Args:
        username = "cli-admin"
        password = "cli-pass-12345"
        display_name = "CLI 管理员"

    assert cmd_create_admin(Args()) == 0
    assert cmd_create_admin(Args()) == 1  # 重复用户名报错
