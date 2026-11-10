#!/usr/bin/env python3
"""Build a reproducible Russian Wikibooks recipe snapshot with licensed images."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

WIKIBOOKS_API = "https://ru.wikibooks.org/w/api.php"
WIKIBOOKS_CATEGORY = "Категория:Рецепты"
USER_AGENT = "KuniniMealieImporter/1.0 (personal self-hosted instance; https://meal.kunini.ru)"
DATA_LICENSE = "CC BY-SA 4.0"
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}

INGREDIENT_HEADING = re.compile(
    r"(?im)^={2,4}\s*(?:состав|ингредиенты|продукты|что нужно)[^=]*={2,4}\s*$"
)
INSTRUCTION_HEADING = re.compile(
    r"(?im)^={2,4}\s*(?:приготовление|способ приготовления|готовим|инструкция|процесс приготовления)[^=]*={2,4}\s*$"
)
IMAGE_PARAMETER = re.compile(r"(?im)^\s*\|\s*Изображение\s*=\s*([^\n|}]+)")
TAG = re.compile(r"<[^>]+>")


def api_request(params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2", "maxlag": "5"})
    request = urllib.request.Request(
        f"{WIKIBOOKS_API}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    for attempt in range(7):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 503) or attempt == 6:
                raise
            delay = int(error.headers.get("Retry-After") or min(2**attempt, 20))
            time.sleep(delay)
        except urllib.error.URLError:
            if attempt == 6:
                raise
            time.sleep(min(2**attempt, 20))
    raise AssertionError("unreachable")


def category_pages() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    continuation: dict[str, str] = {}
    while True:
        response = api_request(
            {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": WIKIBOOKS_CATEGORY,
                "cmnamespace": "104",
                "cmlimit": "500",
                **continuation,
            }
        )
        pages.extend(response["query"]["categorymembers"])
        if "continue" not in response:
            return pages
        continuation = {"cmcontinue": response["continue"]["cmcontinue"]}
        time.sleep(1)


def recipe_pages(page_ids: list[int]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for offset in range(0, len(page_ids), 50):
        response = api_request(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "pageids": "|".join(str(page_id) for page_id in page_ids[offset : offset + 50]),
            }
        )
        pages.extend(response["query"]["pages"])
        time.sleep(1.25)
    return pages


def normalize_image_name(value: str) -> str:
    value = value.strip().replace("_", " ")
    value = re.sub(r"^\[\[(?:Файл|Изображение|File):", "", value, flags=re.IGNORECASE)
    value = value.split("|", 1)[0].removesuffix("]] ").removesuffix("]]" ).strip()
    return value


def plain_metadata(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return html.unescape(TAG.sub("", str(value.get("value") or ""))).strip()


def is_free_license(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip().upper()
    if "NC" in normalized or "ND" in normalized:
        return False
    return any(
        marker in normalized
        for marker in ("PUBLIC DOMAIN", "CC0", "CC BY", "CC-BY", "GFDL", "KOGL TYPE 1")
    )


def image_metadata(names: list[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for offset in range(0, len(names), 50):
        titles = [f"File:{name}" for name in names[offset : offset + 50]]
        response = api_request(
            {
                "action": "query",
                "prop": "imageinfo",
                "iiprop": "url|mime|extmetadata",
                "iiurlwidth": "1600",
                "titles": "|".join(titles),
            }
        )
        for page in response["query"]["pages"]:
            # Shared Commons files are marked as missing in the local Wikibooks
            # repository even though imageinfo is complete and usable.
            if not page.get("imageinfo"):
                continue
            info = page["imageinfo"][0]
            metadata = info.get("extmetadata") or {}
            license_name = plain_metadata(metadata.get("LicenseShortName")) or plain_metadata(
                metadata.get("UsageTerms")
            )
            mime = str(info.get("mime") or "")
            if mime not in IMAGE_MIMES or not is_free_license(license_name):
                continue
            name = page["title"].split(":", 1)[-1].replace("_", " ").casefold()
            result[name] = {
                "url": str(info.get("thumburl") or info["url"]),
                "sourceUrl": str(info.get("descriptionurl") or ""),
                "mime": mime,
                "author": plain_metadata(metadata.get("Artist")),
                "license": license_name,
                "licenseUrl": plain_metadata(metadata.get("LicenseUrl")),
            }
        time.sleep(1.25)
    return result


def build_snapshot(limit: int | None = None) -> dict[str, Any]:
    members = category_pages()
    if limit is not None:
        members = members[:limit]
    pages = recipe_pages([int(page["pageid"]) for page in members])

    candidates: list[dict[str, Any]] = []
    for page in pages:
        revision = page.get("revisions", [{}])[0]
        content = revision.get("slots", {}).get("main", {}).get("content", "")
        image_match = IMAGE_PARAMETER.search(content)
        if not (INGREDIENT_HEADING.search(content) and INSTRUCTION_HEADING.search(content) and image_match):
            continue
        image_name = normalize_image_name(image_match.group(1))
        if not image_name or image_name.casefold() in {"нет", "-"}:
            continue
        candidates.append(
            {
                "pageId": int(page["pageid"]),
                "title": page["title"],
                "revisionId": int(revision["revid"]),
                "revisionTimestamp": revision["timestamp"],
                "sourceUrl": f"https://ru.wikibooks.org/w/index.php?oldid={revision['revid']}",
                "imageName": image_name,
                "wikitext": content,
            }
        )

    images = image_metadata(sorted({recipe["imageName"] for recipe in candidates}, key=str.casefold))
    recipes = []
    for recipe in candidates:
        image = images.get(recipe["imageName"].replace("_", " ").casefold())
        if image:
            recipes.append({**recipe, "image": image})

    return {
        "schemaVersion": 1,
        "fetchedAt": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "source": WIKIBOOKS_API,
        "category": WIKIBOOKS_CATEGORY,
        "dataLicense": DATA_LICENSE,
        "sourcePages": len(members),
        "candidateRecipes": len(candidates),
        "recipes": recipes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = build_snapshot(args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.write(
        json.dumps(
            {
                "sourcePages": snapshot["sourcePages"],
                "candidateRecipes": snapshot["candidateRecipes"],
                "licensedImageRecipes": len(snapshot["recipes"]),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
