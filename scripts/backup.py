from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from pathlib import Path


def docker_output(*args: str) -> bytes:
    result = subprocess.run(
        ["docker", "compose", *args],
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the Docker database and stored images.")
    parser.add_argument("--output", type=Path, default=Path("backups"))
    args = parser.parse_args()

    target = args.output / datetime.now().strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=False)
    database = docker_output(
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom',
    )
    files = docker_output("exec", "-T", "web", "tar", "-C", "/data", "-czf", "-", "files")
    (target / "database.dump").write_bytes(database)
    (target / "files.tar.gz").write_bytes(files)
    print(target.resolve())


if __name__ == "__main__":
    main()
