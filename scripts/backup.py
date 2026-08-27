from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
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


def docker_input(content: bytes, *args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args],
        check=True,
        cwd=PROJECT_DIR,
        input=content,
    )


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
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    base_name = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = output / base_name
    while target.exists():
        target = output / f"{base_name}-{uuid.uuid4().hex[:8]}"
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=output))
    restrict_private_path(staging)
    application_services = [name for name in ("web", "worker") if name in active_services]
    try:
        if "web" in application_services:
            docker_run("stop", "--timeout", "30", "web")
        if "worker" in application_services:
            docker_run("stop", "--timeout", "720", "worker")
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
        (staging / "database.dump").write_bytes(database)
        (staging / "files.tar.gz").write_bytes(files)
        copy_private_file(env_file, staging / "deployment.env")
        _write_manifest(staging)
        verify_backup(staging)
        # Keep the readable timestamp when possible, but never overwrite a
        # concurrent backup that won the same-second name race.
        while True:
            try:
                staging.replace(target)
                break
            except OSError:
                if not target.exists():
                    raise
                target = output / f"{base_name}-{uuid.uuid4().hex[:8]}"
    finally:
        if "web" in application_services:
            docker_run("start", "web")
        if "worker" in application_services:
            docker_run("start", "worker")
        if staging.exists():
            shutil.rmtree(staging)
    return target.resolve()


def verify_backup(backup_dir: Path) -> dict:
    backup_dir = backup_dir.resolve()
    manifest_path = backup_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"备份清单无效：{exc}") from exc
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise RuntimeError("备份清单格式无效")
    for name in ("database.dump", "files.tar.gz", "deployment.env"):
        metadata = manifest["files"].get(name)
        path = backup_dir / name
        if not isinstance(metadata, dict) or not path.is_file():
            raise RuntimeError(f"备份缺少文件：{name}")
        if path.stat().st_size != metadata.get("bytes") or _sha256(path) != metadata.get("sha256"):
            raise RuntimeError(f"备份文件校验失败：{name}")
    _verify_files_archive(backup_dir / "files.tar.gz")
    return manifest


def drill_backup(backup_dir: Path) -> None:
    verify_backup(backup_dir)
    database_name = f"imagegen_restore_{uuid.uuid4().hex[:12]}"
    dump = (backup_dir / "database.dump").read_bytes()
    docker_run(
        "exec",
        "-T",
        "db",
        "sh",
        "-ec",
        f'createdb -U "$POSTGRES_USER" "{database_name}"',
    )
    try:
        docker_input(
            dump,
            "exec",
            "-T",
            "db",
            "sh",
            "-ec",
            f'pg_restore -U "$POSTGRES_USER" -d "{database_name}" --exit-on-error',
        )
        version = docker_output(
            "exec",
            "-T",
            "db",
            "sh",
            "-ec",
            (
                f'psql -U "$POSTGRES_USER" -d "{database_name}" -Atc '
                "'SELECT version_num FROM alembic_version'"
            ),
        ).decode("utf-8", errors="replace")
        if not version.strip():
            raise RuntimeError("恢复演练数据库缺少 Alembic 版本")
    finally:
        docker_run(
            "exec",
            "-T",
            "db",
            "sh",
            "-ec",
            f'dropdb -U "$POSTGRES_USER" --if-exists "{database_name}"',
        )


def mirror_backup(backup_dir: Path, mirror_root: Path) -> Path:
    mirror_root = mirror_root.resolve()
    mirror_root.mkdir(parents=True, exist_ok=True)
    restrict_private_path(mirror_root)
    destination = mirror_root / backup_dir.name
    if destination.exists():
        raise FileExistsError(f"异机备份目录已存在：{destination}")
    shutil.copytree(backup_dir, destination)
    restrict_private_path(destination)
    for path in destination.iterdir():
        restrict_private_path(path)
    verify_backup(destination)
    return destination


def prune_backups(output: Path, retention_days: int) -> int:
    if retention_days < 1 or not output.exists():
        return 0
    cutoff = datetime.now().timestamp() - retention_days * 86400
    root = output.resolve()
    removed = 0
    for candidate in root.iterdir():
        try:
            created = datetime.strptime(candidate.name[:15], "%Y%m%d-%H%M%S").timestamp()
            candidate.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        if candidate.is_dir() and created < cutoff and (candidate / "manifest.json").is_file():
            shutil.rmtree(candidate)
            removed += 1
    return removed


def _write_manifest(target: Path) -> None:
    files = {}
    for name in ("database.dump", "files.tar.gz", "deployment.env"):
        path = target / name
        files[name] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    manifest = {
        "schema": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "files": files,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_files_archive(path: Path) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                relative = Path(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError("图片归档包含越界路径")
                if not relative.parts or relative.parts[0] != "files":
                    raise RuntimeError("图片归档只能包含 files 目录")
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError("图片归档包含链接或特殊文件")
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError(f"图片归档校验失败：{exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="备份 Docker 数据库和已保存的图片。")
    parser.add_argument("--output", type=Path, default=Path("backups"))
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / ".env")
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--skip-drill", action="store_true")
    args = parser.parse_args()
    target = create_backup(args.output, args.env_file)
    if not args.skip_drill:
        drill_backup(target)
    if args.mirror is not None:
        mirror_backup(target, args.mirror)
    prune_backups(args.output, args.retention_days)
    print(target)


if __name__ == "__main__":
    main()
