from __future__ import annotations

import argparse
import os
import stat
import subprocess
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def docker_output(*args: str) -> bytes:
    result = subprocess.run(
        ["docker", "compose", *args],
        check=True,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
    )
    return result.stdout


def docker_run(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, cwd=PROJECT_DIR)


def running_services() -> set[str]:
    output = docker_output("ps", "--services", "--status", "running")
    return {line.strip() for line in output.decode("utf-8").splitlines() if line.strip()}


def copy_private_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"找不到部署环境文件：{source}")
    destination.write_bytes(source.read_bytes())
    restrict_private_path(destination)


def restrict_private_path(path: Path) -> None:
    if os.name != "nt":
        mode = stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR
        os.chmod(path, mode)
        return
    username = os.environ.get("USERNAME", "").strip()
    if not username:
        raise RuntimeError("保护备份文件需要 USERNAME 环境变量")
    domain = os.environ.get("USERDOMAIN", "").strip()
    account = f"{domain}\\{username}" if domain else username
    permissions = "(OI)(CI)F" if path.is_dir() else "F"
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{account}:{permissions}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def create_backup(output: Path, env_file: Path) -> Path:
    if not env_file.is_file():
        raise FileNotFoundError(f"找不到部署环境文件：{env_file}")
    active_services = running_services()
    if "db" not in active_services:
        raise RuntimeError("数据库容器未运行，无法备份")
    target = output / datetime.now().strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    restrict_private_path(target)
    application_services = [name for name in ("worker", "web") if name in active_services]
    try:
        if application_services:
            docker_run("stop", "--timeout", "720", *application_services)
        database = docker_output(
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom',
        )
        files = docker_output(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "web",
            "tar",
            "-C",
            "/data",
            "-czf",
            "-",
            "files",
        )
        (target / "database.dump").write_bytes(database)
        (target / "files.tar.gz").write_bytes(files)
        copy_private_file(env_file, target / "deployment.env")
    finally:
        if application_services:
            docker_run("start", *reversed(application_services))
    return target.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="备份 Docker 数据库和已保存的图片。")
    parser.add_argument("--output", type=Path, default=Path("backups"))
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / ".env")
    args = parser.parse_args()
    print(create_backup(args.output, args.env_file))


if __name__ == "__main__":
    main()
