from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

AWESOME_REVISION = "60b6e1d3ddaf1c982426d6c8181827764c6b2012"
SKILL_REVISION = "ecc9c5420c265f6677edc5f4d255bca02497ef71"

AWESOME_DIRECTIONS = {
    "Architecture & Spaces": "architecture",
    "Brand & Logos": "brand",
    "Characters & People": "character",
    "Charts & Infographics": "infographic",
    "Documents & Publishing": "document",
    "History & Classical Themes": "history",
    "Illustration & Art": "illustration",
    "Other Use Cases": "other",
    "Photography & Realism": "photo",
    "Posters & Typography": "poster",
    "Products & E-commerce": "product",
    "Scenes & Storytelling": "scene",
    "UI & Interfaces": "ui",
}


def git_text(repository: Path, revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), "show", f"{revision}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8")


def git_paths(repository: Path, revision: str, root: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "--name-only", revision, root],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def awesome_cases(repository: Path) -> list[dict[str, object]]:
    payload = json.loads(git_text(repository, AWESOME_REVISION, "data/cases.json"))
    result = []
    for item in payload["cases"]:
        identifier = int(item["id"])
        category = str(item.get("category", "Other Use Cases"))
        result.append(
            {
                "id": f"awesome:{identifier}",
                "source": "awesome-gpt-image-2",
                "title": str(item.get("title", f"Case {identifier}")),
                "prompt": str(item.get("prompt", "")).strip(),
                "category": category,
                "direction_id": AWESOME_DIRECTIONS.get(category, "other"),
                "source_url": str(item.get("githubUrl") or item.get("sourceUrl") or ""),
                "styles": list(item.get("styles") or []),
                "scenes": list(item.get("scenes") or []),
                "attribution": str(item.get("sourceLabel", "")),
            }
        )
    return result


def skill_cases(repository: Path) -> list[dict[str, object]]:
    paths = [
        path
        for path in git_paths(repository, SKILL_REVISION, "skills/gpt-image/references")
        if re.fullmatch(r"skills/gpt-image/references/gallery-(?!index)[a-z0-9-]+\.md", path)
    ]
    result = []
    for path in paths:
        gallery_category = Path(path).stem.removeprefix("gallery-")
        content = git_text(repository, SKILL_REVISION, path)
        for match in re.finditer(
            r"^### No\. (\d+) · (.+?)\r?\n(.*?)(?=^### No\. |\Z)",
            content,
            re.MULTILINE | re.DOTALL,
        ):
            identifier, title, body = int(match.group(1)), match.group(2).strip(), match.group(3)
            prompt_match = re.search(r"```text\r?\n(.*?)\r?\n```", body, re.DOTALL)
            metadata_match = re.search(r"^- Metadata: (.+)$", body, re.MULTILINE)
            if prompt_match is None or metadata_match is None:
                raise RuntimeError(f"无法解析 {path} 中的 Case {identifier}")
            metadata = [part.strip() for part in metadata_match.group(1).split(" · ")]
            attribution = metadata[3] if len(metadata) > 3 else ""
            source_match = re.search(r"Source: \[[^]]+\]\(([^)]+)\)", attribution)
            source_url = (
                source_match.group(1)
                if source_match
                else f"https://github.com/wuyoscar/GPT-Image2-Skill/blob/{SKILL_REVISION}/{path}"
            )
            result.append(
                {
                    "id": f"skill:{identifier}",
                    "source": "gpt-image2-skill",
                    "title": title,
                    "prompt": prompt_match.group(1).strip(),
                    "category": metadata[0],
                    "direction_id": "",
                    "source_url": source_url,
                    "gallery_category": gallery_category,
                    "styles": [],
                    "scenes": [],
                    "attribution": attribution,
                }
            )
    return sorted(result, key=lambda item: int(str(item["id"]).split(":", 1)[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description="从固定版本生成本地创作案例目录。")
    parser.add_argument("--awesome-repo", type=Path, required=True)
    parser.add_argument("--skill-repo", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("imagegen/services/creative/data/case_catalog.json"),
    )
    args = parser.parse_args()
    cases = [*awesome_cases(args.awesome_repo), *skill_cases(args.skill_repo)]
    payload = {
        "version": 1,
        "revision": f"awesome:{AWESOME_REVISION};skill:{SKILL_REVISION}",
        "sources": [
            {
                "id": "awesome-gpt-image-2",
                "repository": "https://github.com/freestylefly/awesome-gpt-image-2",
                "revision": AWESOME_REVISION,
                "license": "MIT",
            },
            {
                "id": "gpt-image2-skill",
                "repository": "https://github.com/wuyoscar/GPT-Image2-Skill",
                "revision": SKILL_REVISION,
                "license": "MIT",
            },
        ],
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(cases)} cases to {args.output}")


if __name__ == "__main__":
    main()
