#!/usr/bin/env python3
"""Download licensed source images and attach them to every Mealie recipe."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "KuniniMealieImporter/1.0 (personal self-hosted instance; https://meal.kunini.ru)"
ALLOWED_IMAGE_HOSTS = {"theunitools.com", "upload.wikimedia.org"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
CONTENT_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
TAG = re.compile(r"<[^>]+>")


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageSource:
    url: str
    source_url: str
    author: str
    license: str
    license_url: str
    origin: str


class MealieApi:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                detail = error.read(2048).decode("utf-8", "replace")
                if error.code in (429, 502, 503, 504) and attempt < 3:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise ApiError(f"{method} {path} failed with HTTP {error.code}: {detail}") from error
            except urllib.error.URLError as error:
                if attempt < 3:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise ApiError(f"{method} {path} failed: {error.reason}") from error
        raise AssertionError("unreachable")

    def all_items(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"page": page, "perPage": 100})
            response = self.request("GET", f"{path}?{query}")
            items.extend(response["items"])
            if page >= response["total_pages"]:
                return items
            page += 1

    def upload_image(self, slug: str, image: bytes, extension: str, content_type: str) -> Any:
        boundary = f"----kunini-{uuid.uuid4().hex}"
        chunks = [
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                f"filename=\"recipe.{extension}\"\r\nContent-Type: {content_type}\r\n\r\n"
            ).encode(),
            image,
            (
                f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"extension\"\r\n\r\n"
                f"{extension}\r\n--{boundary}--\r\n"
            ).encode(),
        ]
        request = urllib.request.Request(
            f"{self.base_url}/api/recipes/{urllib.parse.quote(slug)}/image",
            data=b"".join(chunks),
            method="PUT",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(2048).decode("utf-8", "replace")
            raise ApiError(f"PUT image for {slug} failed with HTTP {error.code}: {detail}") from error


def plain_metadata(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return html.unescape(TAG.sub("", str(value.get("value") or ""))).strip()


def is_free_license(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip().upper()
    if "NC" in normalized or "ND" in normalized:
        return False
    return any(marker in normalized for marker in ("PUBLIC DOMAIN", "CC0", "CC BY", "CC-BY", "GFDL"))


class CommonsResolver:
    def __init__(self):
        self.cache: dict[str, ImageSource | None] = {}

    def request(self, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2", "maxlag": "5"})
        request = urllib.request.Request(f"{COMMONS_API}?{query}", headers={"User-Agent": USER_AGENT})
        for attempt in range(7):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in (429, 503) or attempt == 6:
                    raise
                time.sleep(int(error.headers.get("Retry-After") or min(2**attempt, 20)))
            except urllib.error.URLError:
                if attempt == 6:
                    raise
                time.sleep(min(2**attempt, 20))
        raise AssertionError("unreachable")

    def search(self, query: str) -> ImageSource | None:
        key = query.casefold()
        if key in self.cache:
            return self.cache[key]
        response = self.request(
            {
                "action": "query",
                "generator": "search",
                "gsrsearch": f'filetype:bitmap "{query}" food',
                "gsrnamespace": "6",
                "gsrlimit": "10",
                "prop": "imageinfo",
                "iiprop": "url|mime|extmetadata",
                "iiurlwidth": "1600",
            }
        )
        candidates: list[tuple[int, ImageSource]] = []
        query_tokens = {token for token in re.findall(r"[\w-]+", query.casefold()) if len(token) > 2}
        for page in response.get("query", {}).get("pages", []):
            if not page.get("imageinfo"):
                continue
            info = page["imageinfo"][0]
            mime = str(info.get("mime") or "")
            if mime not in CONTENT_EXTENSIONS:
                continue
            metadata = info.get("extmetadata") or {}
            license_name = plain_metadata(metadata.get("LicenseShortName")) or plain_metadata(
                metadata.get("UsageTerms")
            )
            if not is_free_license(license_name):
                continue
            title_tokens = set(re.findall(r"[\w-]+", page["title"].casefold()))
            score = len(query_tokens & title_tokens)
            candidates.append(
                (
                    score,
                    ImageSource(
                        url=str(info.get("thumburl") or info["url"]),
                        source_url=str(info.get("descriptionurl") or ""),
                        author=plain_metadata(metadata.get("Artist")),
                        license=license_name,
                        license_url=plain_metadata(metadata.get("LicenseUrl")),
                        origin="Wikimedia Commons search",
                    ),
                )
            )
        selected = max(candidates, key=lambda item: item[0])[1] if candidates else None
        self.cache[key] = selected
        time.sleep(1.25)
        return selected


def load_unitools(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    recipes = json.loads(path.read_text())["recipes"]
    by_slug = {recipe["slug"]: recipe for recipe in recipes}
    by_name = {recipe["name"]["ru"].casefold(): recipe for recipe in recipes}
    return by_slug, by_name


def load_wikibooks(path: Path) -> dict[str, dict[str, Any]]:
    recipes = json.loads(path.read_text())["recipes"]
    return {recipe["title"].split(":", 1)[-1].casefold(): recipe for recipe in recipes}


def wikibooks_source(recipe: dict[str, Any]) -> ImageSource:
    image = recipe["image"]
    return ImageSource(
        url=image["url"],
        source_url=image["sourceUrl"],
        author=image["author"],
        license=image["license"],
        license_url=image["licenseUrl"],
        origin="Wikibooks / Wikimedia Commons",
    )


def choose_source(
    recipe: dict[str, Any],
    unitools_by_slug: dict[str, dict[str, Any]],
    unitools_by_name: dict[str, dict[str, Any]],
    wikibooks_by_name: dict[str, dict[str, Any]],
    commons: CommonsResolver,
    allow_commons_search: bool,
) -> ImageSource | None:
    extras = recipe.get("extras") or {}
    if extras.get("wikibooksImageUrl"):
        return ImageSource(
            url=str(extras["wikibooksImageUrl"]),
            source_url=str(extras.get("wikibooksImageSourceUrl") or ""),
            author=str(extras.get("wikibooksImageAuthor") or ""),
            license=str(extras.get("wikibooksImageLicense") or ""),
            license_url=str(extras.get("wikibooksImageLicenseUrl") or ""),
            origin="Wikibooks / Wikimedia Commons",
        )

    unitools = unitools_by_slug.get(str(extras.get("unitoolsSlug") or "")) or unitools_by_name.get(
        recipe["name"].casefold()
    )
    if unitools:
        photo = unitools.get("photo") or {}
        if photo.get("url"):
            return ImageSource(
                url=photo["url"],
                source_url=photo["url"],
                author=str(photo.get("author") or ""),
                license=str(photo.get("license") or ""),
                license_url="",
                origin="UniTools / Wikimedia Commons",
            )

    wikibooks = wikibooks_by_name.get(recipe["name"].casefold())
    if wikibooks:
        return wikibooks_source(wikibooks)

    if unitools and allow_commons_search:
        english_name = str(unitools.get("name", {}).get("en") or recipe["name"])
        return commons.search(english_name)
    return None


def download_image(source: ImageSource) -> tuple[bytes, str, str]:
    parsed = urllib.parse.urlparse(source.url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        raise ValueError(f"Image host is not allowed: {parsed.hostname}")
    request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.hostname not in ALLOWED_IMAGE_HOSTS:
            raise ValueError(f"Image redirect host is not allowed: {final.hostname}")
        content_type = response.headers.get_content_type()
        if content_type not in CONTENT_EXTENSIONS:
            raise ValueError(f"Unsupported image content type: {content_type}")
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > MAX_IMAGE_BYTES:
            raise ValueError(f"Image is too large: {content_length} bytes")
        content = response.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image is too large: more than {MAX_IMAGE_BYTES} bytes")
    if len(content) < 100:
        raise ValueError("Downloaded image is unexpectedly small")
    return content, CONTENT_EXTENSIONS[content_type], content_type


def attribution_note(source: ImageSource) -> dict[str, str]:
    text = (
        f"Источник: {source.origin}\n"
        f"Автор: {source.author or 'указан на странице источника'}\n"
        f"Лицензия: {source.license}"
    )
    if source.source_url:
        text += f"\n\n{source.source_url}"
    if source.license_url:
        text += f"\n{source.license_url}"
    return {"title": "Изображение и лицензия", "text": text}


def sync_images(
    api: MealieApi,
    unitools_by_slug: dict[str, dict[str, Any]],
    unitools_by_name: dict[str, dict[str, Any]],
    wikibooks_by_name: dict[str, dict[str, Any]],
    allow_commons_search: bool,
    limit: int | None = None,
) -> dict[str, Any]:
    commons = CommonsResolver()
    summaries = api.all_items("/api/recipes")
    selected = summaries[:limit]
    uploaded = 0
    skipped = 0
    metadata_updated = 0
    missing: list[str] = []
    failures: list[str] = []
    total_bytes = 0
    source_counts: dict[str, int] = {}
    for index, summary in enumerate(selected, 1):
        slug = summary["slug"]
        recipe = api.request("GET", f"/api/recipes/{urllib.parse.quote(slug)}")
        has_attribution = bool((recipe.get("extras") or {}).get("imageLicense")) and any(
            note.get("title") == "Изображение и лицензия" for note in recipe.get("notes", [])
        )
        if summary.get("image") and has_attribution:
            skipped += 1
            continue
        source = choose_source(
            recipe,
            unitools_by_slug,
            unitools_by_name,
            wikibooks_by_name,
            commons,
            allow_commons_search,
        )
        if source is None:
            missing.append(recipe["name"])
            continue
        extras = {**(recipe.get("extras") or {})}
        extras.update(
            {
                "imageSourceUrl": source.source_url or source.url,
                "imageAuthor": source.author,
                "imageLicense": source.license,
                "imageLicenseUrl": source.license_url,
                "imageOrigin": source.origin,
            }
        )
        notes = [
            {"title": note.get("title") or "", "text": note.get("text") or ""}
            for note in recipe.get("notes", [])
            if note.get("title") != "Изображение и лицензия"
        ]
        notes.append(attribution_note(source))
        if summary.get("image"):
            try:
                api.request(
                    "PATCH",
                    f"/api/recipes/{urllib.parse.quote(slug)}",
                    {"extras": extras, "notes": notes},
                )
            except Exception as error:
                failures.append(f"{recipe['name']}: {error}")
                continue
            metadata_updated += 1
            source_counts[source.origin] = source_counts.get(source.origin, 0) + 1
            continue
        try:
            content, extension, content_type = download_image(source)
            api.upload_image(slug, content, extension, content_type)
            api.request("PATCH", f"/api/recipes/{urllib.parse.quote(slug)}", {"extras": extras, "notes": notes})
        except Exception as error:  # keep processing and report every failed recipe
            failures.append(f"{recipe['name']}: {error}")
            continue
        uploaded += 1
        total_bytes += len(content)
        source_counts[source.origin] = source_counts.get(source.origin, 0) + 1
        if index % 25 == 0:
            sys.stderr.write(f"Processed {index}/{len(selected)} recipe images\n")
            sys.stderr.flush()
    return {
        "recipes": len(selected),
        "uploaded": uploaded,
        "skippedWithImage": skipped,
        "metadataUpdated": metadata_updated,
        "missing": missing,
        "failures": failures,
        "downloadedBytes": total_bytes,
        "sources": source_counts,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unitools-dataset", required=True, type=Path)
    parser.add_argument("--wikibooks-dataset", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:9001")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--resolve-missing-from-commons", action="store_true")
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token_file.read_text().strip()
    if not token:
        raise ValueError("Token file is empty")
    unitools_by_slug, unitools_by_name = load_unitools(args.unitools_dataset)
    wikibooks_by_name = load_wikibooks(args.wikibooks_dataset)
    result = sync_images(
        MealieApi(args.base_url, token),
        unitools_by_slug,
        unitools_by_name,
        wikibooks_by_name,
        args.resolve_missing_from_commons,
        args.limit,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 1 if result["missing"] or result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
