"""Local setup: preserve existing keys/accounts; create the first admin interactively."""

import argparse
import getpass
import secrets

from sqlalchemy import select

from gongxu.config import PROJECT_ROOT, get_settings
from gongxu.db.models import User, UserRole
from gongxu.db.session import get_session_factory, init_db
from gongxu.security.auth import AuthError, create_user


def initialize_env() -> None:
    destination = PROJECT_ROOT / ".env"
    if destination.exists():
        return
    settings = get_settings()
    dev_key = settings.data_root / "runtime" / "dev_master.key"
    if settings.master_key_hex:
        master_key = settings.master_key_hex
    elif dev_key.is_file():
        key = dev_key.read_bytes()
        if len(key) != 32:
            raise ValueError("现有开发主密钥无效，请修复后再初始化")
        master_key = key.hex()
    elif any((settings.data_root / "assets" / "encrypted").glob("*.bin")):
        raise ValueError("已有加密视频但没有主密钥，请恢复原密钥后再启动")
    else:
        master_key = secrets.token_hex(32)
    template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    template = template.replace(
        "GONGXU_SESSION_SECRET=change-me-in-production",
        "GONGXU_SESSION_SECRET=" + secrets.token_hex(32),
    ).replace("GONGXU_MASTER_KEY_HEX=\n", f"GONGXU_MASTER_KEY_HEX={master_key}\n")
    # Exclusive creation: a second setup cannot overwrite the first setup's keys.
    with destination.open("x", encoding="utf-8", newline="\n") as output:
        output.write(template)
    get_settings.cache_clear()
    print("已生成本地 .env。模型地址与 API Key 请在该文件中配置。")


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 SOP 陪练的本地配置与首个管理员")
    parser.add_argument("--check-admin", action="store_true", help="只检查是否已有可用管理员")
    args = parser.parse_args()
    if not args.check_admin:
        initialize_env()
    init_db()
    with get_session_factory()() as db:
        existing = db.scalar(select(User).where(User.role == UserRole.admin, User.is_active))
        if args.check_admin:
            raise SystemExit(0 if existing else 2)
        if existing:
            print(f"管理员 {existing.username} 已存在，保留原账号。")
            return
        username = input("管理员用户名（默认 admin）：").strip() or "admin"
        password = getpass.getpass("设置登录密码（至少 8 位，不会显示）：")
        confirmation = getpass.getpass("再次输入密码：")
        if password != confirmation:
            raise SystemExit("两次密码不同，未创建账号。")
        try:
            create_user(
                db, username=username, password=password, role=UserRole.admin, display_name="教师"
            )
        except AuthError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"管理员 {username} 已创建。")


if __name__ == "__main__":
    main()
