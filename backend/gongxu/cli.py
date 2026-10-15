"""CLI 工具：数据库初始化与运维命令。

用法：
  uv run python -m gongxu.cli create-admin --username admin --password <pw> [--display-name 管理员]

生产环境必须通过环境变量传递密码，禁止硬编码。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import cast

from sqlalchemy.orm import Session

from gongxu.db.models import UserRole
from gongxu.db.session import get_session_factory, init_db
from gongxu.security import auth


def _get_db() -> Session:
    init_db()
    return get_session_factory()()


def cmd_create_admin(args: argparse.Namespace) -> int:
    db = _get_db()
    try:
        password = args.password or getpass.getpass("新管理员密码: ")
        user = auth.create_user(
            db,
            username=args.username,
            password=password,
            role=UserRole.admin,
            display_name=args.display_name,
        )
        print(f"已创建管理员: {user.username} (id={user.id})")
        return 0
    except auth.AuthError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def cmd_import_impact_sample(args: argparse.Namespace) -> int:
    """把本地 IMPACT sample 视频注册为项目资产（幂等：已存在则跳过）。

    用途：将官方真实工业装配视频登记为仅限本地处理的研究资产。
    注册后需在 UI 的「知识导入」页点「处理」生成转码/关键帧。
    """
    from gongxu.assets import service as asset_service
    from gongxu.config import get_settings
    from gongxu.data.manifest import sha256_file
    from gongxu.db.models import SourceAsset

    db = _get_db()
    try:
        root = (
            get_settings().data_root / "raw" / "impact-v1.1" / "IMPACT-v1.1" / "sample" / "videos"
        )
        pattern = f"{args.view}/*.mp4" if args.view else "*/**/*.mp4"
        videos = sorted(root.glob(pattern))
        if not videos:
            print(f"未找到视频（{root}）；请先确认 sample 已下载", file=sys.stderr)
            return 1
        imported = 0
        skipped = 0
        for video in videos:
            checksum = sha256_file(video)
            existing = db.query(SourceAsset).filter_by(sha256=checksum).first()
            if existing is not None:
                skipped += 1
                continue
            asset_service.register_asset(
                db,
                kind="video",
                filename=f"IMPACT_{video.parent.name}_{video.stem}.mp4",
                temp_path=video,
                license_note="IMPACT v1.1 (CC BY-NC-SA 4.0) 非商业研究",
                authorized_by=None,
                sha256=checksum,
                # 公开许可不自动等于第三方 API 处理授权；保持禁止外发。
                allow_provider_export=False,
            )
            imported += 1
        print(f"导入完成：新增 {imported} 个，已存在跳过 {skipped} 个")
        print("下一步：在「知识导入」页点「处理」生成关键帧，然后可在 SOP 编译室/考评中心使用。")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="gongxu.cli", description="工序智传 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_admin = sub.add_parser("create-admin", help="创建初始管理员账号")
    p_admin.add_argument("--username", required=True)
    p_admin.add_argument("--password", default="", help="留空则交互输入")
    p_admin.add_argument("--display-name", default="管理员")
    p_admin.set_defaults(func=cmd_create_admin)

    p_impact = sub.add_parser(
        "import-impact-sample", help="导入本地 IMPACT sample 视频为资产（用于测试）"
    )
    p_impact.add_argument("--view", default="", help="只导入某视角（ego/front/left/right/top）")
    p_impact.set_defaults(func=cmd_import_impact_sample)

    args = parser.parse_args()
    return cast(int, args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
