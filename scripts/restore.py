from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

try:
    from scripts.backup import (
        PROJECT_DIR,
        copy_private_file,
        docker_input,
        docker_run,
        drill_backup,
        restrict_private_path,
        running_services,
        verify_backup,
    )
except ModuleNotFoundError:
    from backup import (  # type: ignore[no-redef]
        PROJECT_DIR,
        copy_private_file,
        docker_input,
        docker_run,
        drill_backup,
        restrict_private_path,
        running_services,
        verify_backup,
    )

RESTORED_ENV_KEYS = ("SECRET_KEY", "CONFIG_ENCRYPTION_KEY")


def terminate_database_sessions() -> None:
    """Close stale application connections before replacing the database contents."""
    docker_run(
        "exec",
        "-T",
        "db",
        "sh",
        "-ec",
        (
            'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '
            '"SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
            'WHERE datname = current_database() AND pid <> pg_backend_pid();"'
        ),
    )


def _env_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def restore_deployment_keys(source: Path, destination: Path) -> None:
    source_lines = source.read_text(encoding="utf-8-sig").splitlines()
    destination_lines = (
        destination.read_text(encoding="utf-8-sig").splitlines() if destination.exists() else []
    )
    restored = {key: _env_value(source_lines, key) for key in RESTORED_ENV_KEYS}
    if any(value is None for value in restored.values()):
        raise RuntimeError("备份部署环境缺少 SECRET_KEY 或 CONFIG_ENCRYPTION_KEY")

    merged: list[str] = []
    replaced: set[str] = set()
    for line in destination_lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in restored:
            if key not in replaced:
                merged.append(f"{key}={restored[key]}")
                replaced.add(key)
            continue
        merged.append(line)
    for key in RESTORED_ENV_KEYS:
        if key not in replaced:
            merged.append(f"{key}={restored[key]}")
    destination.write_text("\n".join(merged) + "\n", encoding="utf-8")
    restrict_private_path(destination)


def restore_backup(
    backup_dir: Path,
    env_file: Path,
    *,
    replace_env: bool = False,
) -> None:
    backup_dir = backup_dir.resolve()
    verify_backup(backup_dir)
    active_services = running_services()
    if "db" not in active_services:
        raise RuntimeError("数据库容器未运行，无法恢复")
    if "migrate" in active_services:
        raise RuntimeError("迁移容器正在运行，无法在并发写入时恢复")
    application_services = [name for name in ("web", "worker") if name in active_services]
    if "web" in application_services:
        docker_run("stop", "--timeout", "30", "web")
    if "worker" in application_services:
        docker_run("stop", "--timeout", "720", "worker")
    try:
        terminate_database_sessions()
        if replace_env:
            if env_file.exists():
                backup_name = f"{env_file.name}.pre-restore-{datetime.now():%Y%m%d-%H%M%S}"
                copy_private_file(env_file, env_file.with_name(backup_name))
            restore_deployment_keys(backup_dir / "deployment.env", env_file)
        docker_input(
            (backup_dir / "database.dump").read_bytes(),
            "exec",
            "-T",
            "db",
            "sh",
            "-ec",
            (
                'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                "--clean --if-exists --no-owner --no-privileges --exit-on-error"
            ),
        )
        docker_input(
            (backup_dir / "files.tar.gz").read_bytes(),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "web",
            "sh",
            "-ec",
            "rm -rf /data/files && mkdir -p /data/files && tar -C /data -xzf -",
        )
        docker_run("run", "--rm", "--no-deps", "migrate")
    except Exception:
        raise RuntimeError(
            "恢复失败，Web 与 Worker 保持停止；请检查数据库和文件卷后再手动启动"
        ) from None
    if replace_env and application_services:
        docker_run("up", "-d", "--no-deps", "--force-recreate", *application_services)
    else:
        for service in application_services:
            docker_run("start", service)


def main() -> None:
    parser = argparse.ArgumentParser(description="校验、演练或恢复 Snow AI Studio 备份。")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / ".env")
    parser.add_argument("--replace-env", action="store_true")
    parser.add_argument("--drill", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.drill:
        drill_backup(args.backup_dir)
        print("恢复演练通过")
        return
    if args.confirm != "RESTORE":
        raise SystemExit("实际恢复必须显式传入 --confirm RESTORE")
    restore_backup(args.backup_dir, args.env_file, replace_env=args.replace_env)
    print("恢复完成")


if __name__ == "__main__":
    main()
