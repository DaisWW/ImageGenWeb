"""Call the Snow AI Studio generation API with Python's standard library."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

TERMINAL_STATES = {"succeeded", "partial", "failed", "canceled"}
IMAGE_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GenerationApiClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self._api_key = api_key
        self._opener = build_opener(NoRedirect())

    def open_response(
        self, path: str, *, body: bytes | None = None, request_id: UUID | None = None
    ) -> HTTPResponse:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if request_id is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": str(request_id)})
        request = Request(f"{self.base_url}/api/v1/{path}", data=body, headers=headers)
        try:
            return self._opener.open(request, timeout=30)
        except HTTPError as error:
            with error:
                try:
                    details = json.loads(error.read(4096))
                except ValueError:
                    details = {}
            message = f"HTTP {error.code} for {request.get_method()} {path}"
            if isinstance(details, dict) and isinstance(details.get("error"), str):
                message += " " + details["error"].replace(self._api_key, "[redacted]")
            raise RuntimeError(message) from None

    def request_json(
        self, path: str, *, body: bytes | None = None, request_id: UUID | None = None
    ) -> dict:
        with self.open_response(path, body=body, request_id=request_id) as response:
            return json.load(response)

    def get_job(self, job_id: str) -> dict:
        return self.request_json(f"generations/{job_id}")["job"]


def wait_for_job(client: GenerationApiClient, job_id: str, job: dict) -> dict:
    deadline = time.monotonic() + 600
    while job["status"] not in TERMINAL_STATES:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Job still active. Resume with wait {job_id}.")
        time.sleep(3)
        job = client.get_job(job_id)
    return job


def download_images(
    client: GenerationApiClient, job_id: str, items: list[dict], output_dir: Path
) -> list[str]:
    files = []
    output_dir = output_dir.resolve()
    for item in items:
        if item["status"] != "succeeded":
            continue
        item_id = UUID(item["id"]).hex
        mime_type = item["mime_type"]
        extension = IMAGE_EXTENSIONS[mime_type]
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"image_{item_id}.{extension}"
        with client.open_response(f"generations/{job_id}/images/{item_id}") as response:
            save_image(response, path, mime_type)
        files.append(str(path))
    return files


def save_image(response: HTTPResponse, path: Path, mime_type: str) -> None:
    if response.headers.get_content_type() != mime_type:
        raise ValueError("Unexpected image Content-Type.")
    content_length = response.headers.get("Content-Length")
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        partial = Path(directory) / path.name
        with partial.open("wb") as file:
            shutil.copyfileobj(response, file)
            received = file.tell()
        if not received or (content_length is not None and received != int(content_length)):
            raise OSError("Incomplete image download.")
        partial.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("snow-images"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="List available channels, prices and request limits")
    generate = commands.add_parser("generate", help="Submit a JSON request and download its images")
    generate.add_argument("request_file", type=Path)
    generate.add_argument("--request-id", type=UUID, help="Reuse the UUID of the same submission")
    wait = commands.add_parser("wait", help="Resume waiting for an existing job")
    wait.add_argument("job_id", type=UUID)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    base_url = os.environ.get("SNOW_IMAGE_API_URL", "").strip().rstrip("/")
    api_key = os.environ.get("SNOW_IMAGE_API_KEY", "").strip()
    if not base_url or not api_key:
        parser.error("Set SNOW_IMAGE_API_URL and SNOW_IMAGE_API_KEY in the environment.")
    address = urlsplit(base_url)
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.username is not None
        or address.query
        or address.fragment
    ):
        parser.error("SNOW_IMAGE_API_URL must be an HTTP(S) site URL.")
    client = GenerationApiClient(base_url, api_key)

    try:
        if args.command == "config":
            print(json.dumps(client.request_json("config"), indent=2))
            return 0

        if args.command == "generate":
            request_id = args.request_id or uuid4()
            payload = json.loads(args.request_file.read_text(encoding="utf-8-sig"))
            print(f"Request ID: {request_id}", file=sys.stderr, flush=True)
            request_body = json.dumps(payload).encode()
            job = client.request_json("generations", body=request_body, request_id=request_id)[
                "job"
            ]
            job_id = UUID(job["id"]).hex
        else:
            job_id = args.job_id.hex
            job = client.get_job(job_id)

        print(f"Job ID: {job_id}", file=sys.stderr, flush=True)
        job = wait_for_job(client, job_id, job)
        files = download_images(client, job_id, job["items"], args.output_dir)

        print(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": job["status"],
                    "charged_rmb": job["charged_rmb"],
                    "files": files,
                    "errors": [item["error_code"] for item in job["items"] if item["error_code"]],
                },
                indent=2,
            )
        )
        return 0 if files else 1
    except (HTTPException, OSError, RuntimeError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
