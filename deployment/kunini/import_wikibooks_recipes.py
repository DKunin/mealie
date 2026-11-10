#!/usr/bin/env python3
"""Import a Russian Wikibooks snapshot into Mealie as structured recipes."""

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_LICENSE = "CC BY-SA 4.0"
SOURCE_NAME = "Русская книга рецептов Wikibooks"
SOURCE_TAG = "Wikibooks CC BY-SA 4.0"

HEADING = re.compile(r"(?m)^(={2,4})\s*(.*?)\s*\1\s*$")
INGREDIENT_NAMES = {"состав", "ингредиенты", "продукты", "что нужно"}
INSTRUCTION_NAMES = {"приготовление", "способ приготовления", "готовим", "инструкция", "процесс приготовления"}
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
REF = re.compile(r"<ref\b[^>]*>.*?</ref\s*>|<ref\b[^>]*/\s*>", re.IGNORECASE | re.DOTALL)
TEMPLATE = re.compile(r"\{\{[^{}]*}}")
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?]]")
EXTERNAL_LINK = re.compile(r"\[(?:https?://\S+)(?:\s+([^\]]+))?]")
HTML_TAG = re.compile(r"<[^>]+>")
LIST_MARKER = re.compile(r"^[*#;:]+\s*")
TEMPLATE_PARAMETER = re.compile(r"(?im)^\s*\|\s*([^=\n]+?)\s*=\s*([^\n|}]*)")

UNICODE_FRACTIONS = {"½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75, "⅛": 0.125}
UNIT_ALIASES = {
    "кг": ("килограмм", "кг", False),
    "килограмм": ("килограмм", "кг", False),
    "килограмма": ("килограмм", "кг", False),
    "килограммов": ("килограмм", "кг", False),
    "г": ("грамм", "г", False),
    "гр": ("грамм", "г", False),
    "грамм": ("грамм", "г", False),
    "грамма": ("грамм", "г", False),
    "граммов": ("грамм", "г", False),
    "мл": ("миллилитр", "мл", False),
    "миллилитр": ("миллилитр", "мл", False),
    "миллилитра": ("миллилитр", "мл", False),
    "л": ("литр", "л", False),
    "литр": ("литр", "л", False),
    "литра": ("литр", "л", False),
    "ст. л.": ("столовая ложка", "ст. л.", True),
    "ст. л": ("столовая ложка", "ст. л.", True),
    "ст.л.": ("столовая ложка", "ст. л.", True),
    "ст.л": ("столовая ложка", "ст. л.", True),
    "столовая ложка": ("столовая ложка", "ст. л.", True),
    "столовые ложки": ("столовая ложка", "ст. л.", True),
    "столовых ложек": ("столовая ложка", "ст. л.", True),
    "ч. л.": ("чайная ложка", "ч. л.", True),
    "ч. л": ("чайная ложка", "ч. л.", True),
    "ч.л.": ("чайная ложка", "ч. л.", True),
    "ч.л": ("чайная ложка", "ч. л.", True),
    "чайная ложка": ("чайная ложка", "ч. л.", True),
    "чайные ложки": ("чайная ложка", "ч. л.", True),
    "чайных ложек": ("чайная ложка", "ч. л.", True),
    "шт": ("штука", "шт.", True),
    "шт.": ("штука", "шт.", True),
    "штука": ("штука", "шт.", True),
    "штуки": ("штука", "шт.", True),
    "штук": ("штука", "шт.", True),
    "зубчик": ("зубчик", "зуб.", True),
    "зубчика": ("зубчик", "зуб.", True),
    "зубчиков": ("зубчик", "зуб.", True),
    "стакан": ("стакан", "стак.", True),
    "стакана": ("стакан", "стак.", True),
    "стаканов": ("стакан", "стак.", True),
    "чашка": ("чашка", "чаш.", True),
    "чашки": ("чашка", "чаш.", True),
    "чашек": ("чашка", "чаш.", True),
    "щепотка": ("щепотка", "", True),
    "пучок": ("пучок", "", True),
    "банка": ("банка", "", True),
    "пакет": ("пакет", "", True),
    "ломтик": ("ломтик", "", True),
    "кусок": ("кусок", "", True),
}
UNIT_PATTERN = "|".join(sorted((re.escape(alias) for alias in UNIT_ALIASES), key=len, reverse=True))
LEADING_AMOUNT = re.compile(
    rf"^\s*(\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[½⅓⅔¼¾⅛])(?:\s*[-–—]\s*(\d+(?:[.,]\d+)?))?\s*(?:({UNIT_PATTERN})(?=\s|$))?\s*(.*)$",
    re.IGNORECASE,
)
TRAILING_AMOUNT = re.compile(
    rf"^(.+?)(?:\s+[—–-]\s+|:\s*)(\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[½⅓⅔¼¾⅛])"
    rf"(?:\s*[-–—]\s*(\d+(?:[.,]\d+)?))?\s*(?:({UNIT_PATTERN})(?=\s|$))?\s*$",
    re.IGNORECASE,
)


class ApiError(RuntimeError):
    pass


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
                with urllib.request.urlopen(request, timeout=60) as response:
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
            response = self.request("GET", f"{path}?{urllib.parse.urlencode({'page': page, 'perPage': 100})}")
            items.extend(response["items"])
            if page >= response["total_pages"]:
                return items
            page += 1


@dataclass(frozen=True)
class ParsedIngredient:
    food: str
    quantity: float
    unit: str | None
    original: str


@dataclass(frozen=True)
class ParsedRecipe:
    source: dict[str, Any]
    name: str
    description: str
    category: str
    cuisine: str
    servings: float
    total_time_seconds: int | None
    ingredients: list[ParsedIngredient]
    instructions: list[str]


def clean_markup(value: str) -> str:
    value = COMMENT.sub("", value)
    value = REF.sub("", value)
    for _ in range(5):
        updated = TEMPLATE.sub("", value)
        if updated == value:
            break
        value = updated
    value = WIKILINK.sub(lambda match: match.group(2) or match.group(1).split(":")[-1], value)
    value = EXTERNAL_LINK.sub(lambda match: match.group(1) or "", value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = HTML_TAG.sub("", value)
    value = value.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip(" \t\n.;")


def heading_name(value: str) -> str:
    return clean_markup(value).casefold().rstrip(":")


def section_lines(value: str) -> list[str]:
    result = []
    for raw in value.splitlines():
        if not LIST_MARKER.match(raw):
            continue
        cleaned = clean_markup(LIST_MARKER.sub("", raw))
        if cleaned:
            result.append(cleaned)
    return result


def parse_number(value: str) -> float:
    value = value.strip()
    if value in UNICODE_FRACTIONS:
        return UNICODE_FRACTIONS[value]
    if "/" in value:
        numerator, denominator = value.replace(" ", "").split("/", 1)
        return float(numerator) / float(denominator)
    return float(value.replace(",", "."))


def clean_food_name(value: str) -> str:
    value = value.strip(" ,.;:—–-")
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    value = re.split(r"\s*[,;]\s*", value, maxsplit=1)[0]
    value = re.sub(r"^(?:около|примерно|приблизительно)\s+", "", value, flags=re.IGNORECASE)
    return value.strip(" ,.;:—–-")


def parse_ingredient(value: str) -> ParsedIngredient | None:
    original = clean_markup(value)
    if not original:
        return None

    match = LEADING_AMOUNT.match(original)
    if match:
        quantity = parse_number(match.group(1))
        if match.group(2):
            quantity = (quantity + parse_number(match.group(2))) / 2
        unit_alias = (match.group(3) or "").casefold()
        food = clean_food_name(match.group(4))
    else:
        match = TRAILING_AMOUNT.match(original)
        if match:
            food = clean_food_name(match.group(1))
            quantity = parse_number(match.group(2))
            if match.group(3):
                quantity = (quantity + parse_number(match.group(3))) / 2
            unit_alias = (match.group(4) or "").casefold()
        else:
            food = clean_food_name(re.sub(r"\s*[—–,:-]?\s*по вкусу\s*$", "", original, flags=re.IGNORECASE))
            quantity = 0
            unit_alias = "по вкусу" if re.search(r"по вкусу", original, re.IGNORECASE) else ""

    if not food or len(food) < 2:
        return None
    unit = UNIT_ALIASES[unit_alias][0] if unit_alias in UNIT_ALIASES else ("по вкусу" if unit_alias else None)
    return ParsedIngredient(food=food, quantity=round(quantity, 3), unit=unit, original=original)


def template_parameters(wikitext: str) -> dict[str, str]:
    return {clean_markup(key).casefold(): clean_markup(value) for key, value in TEMPLATE_PARAMETER.findall(wikitext)}


def parse_duration(value: str) -> int | None:
    hours = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ч|час)", value, re.IGNORECASE)
    minutes = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мин)", value, re.IGNORECASE)
    total = 0.0
    if hours:
        total += float(hours.group(1).replace(",", ".")) * 3600
    if minutes:
        total += float(minutes.group(1).replace(",", ".")) * 60
    return int(total) if total else None


def parse_recipe(source: dict[str, Any]) -> ParsedRecipe | None:
    wikitext = source["wikitext"]
    headings = list(HEADING.finditer(wikitext))
    ingredient_index = next(
        (index for index, item in enumerate(headings) if heading_name(item.group(2)) in INGREDIENT_NAMES), None
    )
    if ingredient_index is None:
        return None
    instruction_index = next(
        (
            index
            for index, item in enumerate(headings[ingredient_index + 1 :], ingredient_index + 1)
            if heading_name(item.group(2)) in INSTRUCTION_NAMES
        ),
        None,
    )
    if instruction_index is None:
        return None

    ingredient_heading = headings[ingredient_index]
    instruction_heading = headings[instruction_index]
    ingredient_text = wikitext[ingredient_heading.end() : instruction_heading.start()]
    instruction_end = len(wikitext)
    instruction_level = len(instruction_heading.group(1))
    for item in headings[instruction_index + 1 :]:
        if len(item.group(1)) <= instruction_level:
            instruction_end = item.start()
            break
    instruction_text = wikitext[instruction_heading.end() : instruction_end]

    ingredients = [item for line in section_lines(ingredient_text) if (item := parse_ingredient(line))]
    instructions = section_lines(instruction_text)
    if len(ingredients) < 2 or not instructions:
        return None

    parameters = template_parameters(wikitext[: ingredient_heading.start()])
    name = source["title"].split(":", 1)[-1].strip()
    intro_start = wikitext.find("}}") + 2
    description = clean_markup(wikitext[intro_start : ingredient_heading.start()])
    description = description[:1000]
    servings_match = re.search(r"\d+(?:[.,]\d+)?", parameters.get("порций", ""))
    servings = float(servings_match.group().replace(",", ".")) if servings_match else 0
    return ParsedRecipe(
        source=source,
        name=name,
        description=description,
        category=parameters.get("категория", "Рецепты Wikibooks") or "Рецепты Wikibooks",
        cuisine=parameters.get("кухня", ""),
        servings=servings,
        total_time_seconds=parse_duration(parameters.get("время", "")),
        ingredients=ingredients,
        instructions=instructions,
    )


def load_snapshot(path: Path) -> tuple[dict[str, Any], list[ParsedRecipe]]:
    data = json.loads(path.read_text())
    if data.get("schemaVersion") != 1 or data.get("dataLicense") != DATA_LICENSE:
        raise ValueError("Expected a Russian Wikibooks schemaVersion 1 snapshot under CC BY-SA 4.0")
    parsed = [recipe for source in data.get("recipes", []) if (recipe := parse_recipe(source))]
    if not parsed:
        raise ValueError("Snapshot contains no importable recipes")
    return data, parsed


def ensure_named_items(api: MealieApi, path: str, names: set[str]) -> dict[str, dict[str, Any]]:
    existing = api.all_items(path)
    by_name = {item["name"].casefold(): item for item in existing}
    for name in sorted(names, key=str.casefold):
        key = name.casefold()
        if key not in by_name:
            by_name[key] = api.request("POST", path, {"name": name})
    return {name.casefold(): by_name[name.casefold()] for name in names}


def ensure_units(api: MealieApi, names: set[str]) -> dict[str, dict[str, Any]]:
    existing = api.all_items("/api/units")
    by_name = {item["name"].casefold(): item for item in existing}
    definitions = {definition[0]: definition for definition in UNIT_ALIASES.values()}
    definitions["по вкусу"] = ("по вкусу", "", True)
    for name in sorted(names, key=str.casefold):
        if name.casefold() in by_name:
            continue
        _, abbreviation, fraction = definitions[name]
        by_name[name.casefold()] = api.request(
            "POST",
            "/api/units",
            {
                "name": name,
                "abbreviation": abbreviation,
                "useAbbreviation": bool(abbreviation),
                "fraction": fraction,
                "extras": {"wikibooksUnit": True},
            },
        )
    return {name.casefold(): by_name[name.casefold()] for name in names}


def ensure_foods(api: MealieApi, names: set[str]) -> dict[str, dict[str, Any]]:
    existing = api.all_items("/api/foods")
    by_name = {item["name"].casefold(): item for item in existing}
    for name in sorted(names, key=str.casefold):
        if name.casefold() not in by_name:
            by_name[name.casefold()] = api.request(
                "POST",
                "/api/foods",
                {"name": name, "description": f"Продукт из {SOURCE_NAME}", "extras": {"wikibooks": True}},
            )
    return {name.casefold(): by_name[name.casefold()] for name in names}


def recipe_payload(
    recipe: ParsedRecipe,
    foods: dict[str, dict[str, Any]],
    units: dict[str, dict[str, Any]],
    categories: dict[str, dict[str, Any]],
    tags: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source = recipe.source
    image = source["image"]
    category_names = [recipe.category]
    if recipe.cuisine and recipe.cuisine.casefold() != recipe.category.casefold():
        category_names.append(recipe.cuisine)
    attribution = (
        f"Текст: {SOURCE_NAME}, редакция {source['revisionId']} ({DATA_LICENSE}).\n"
        f"Изображение: {image['author'] or 'автор указан на странице файла'} — {image['license']}."
    )
    return {
        "name": recipe.name,
        "recipeServings": recipe.servings,
        "recipeYieldQuantity": recipe.servings,
        "recipeYield": "порций" if recipe.servings else "",
        "totalTimeSeconds": recipe.total_time_seconds,
        "description": f"{recipe.description}\n\nИсточник: {SOURCE_NAME}. Лицензия: {DATA_LICENSE}.".strip(),
        "recipeCategory": [categories[name.casefold()] for name in category_names],
        "tags": [tags[SOURCE_TAG.casefold()]],
        "orgURL": source["sourceUrl"],
        "recipeIngredient": [
            {
                "quantity": ingredient.quantity,
                "food": {
                    "id": foods[ingredient.food.casefold()]["id"],
                    "name": foods[ingredient.food.casefold()]["name"],
                },
                "unit": (
                    {"id": units[ingredient.unit.casefold()]["id"], "name": units[ingredient.unit.casefold()]["name"]}
                    if ingredient.unit
                    else None
                ),
                "note": f"Исходная строка: {ingredient.original}",
                "originalText": ingredient.original,
            }
            for ingredient in recipe.ingredients
        ],
        "recipeInstructions": [{"text": step} for step in recipe.instructions],
        "notes": [
            {
                "title": "Источник и лицензия",
                "text": f"{attribution}\n\n{source['sourceUrl']}\n{image['sourceUrl']}\n{image['licenseUrl']}",
            }
        ],
        "extras": {
            "wikibooksPageId": str(source["pageId"]),
            "wikibooksRevisionId": str(source["revisionId"]),
            "wikibooksLicense": DATA_LICENSE,
            "wikibooksImageUrl": image["url"],
            "wikibooksImageSourceUrl": image["sourceUrl"],
            "wikibooksImageAuthor": image["author"],
            "wikibooksImageLicense": image["license"],
            "wikibooksImageLicenseUrl": image["licenseUrl"],
        },
    }


def import_recipes(api: MealieApi, recipes: list[ParsedRecipe], limit: int | None = None) -> dict[str, int]:
    selected = recipes[:limit]
    existing = api.all_items("/api/recipes")
    existing_by_name = {item["name"].casefold(): item for item in existing}
    existing_page_ids: set[str] = set()
    for item in existing:
        full = api.request("GET", f"/api/recipes/{urllib.parse.quote(item['slug'])}")
        page_id = str(full.get("extras", {}).get("wikibooksPageId") or "")
        if page_id:
            existing_page_ids.add(page_id)

    pending = [
        recipe
        for recipe in selected
        if str(recipe.source["pageId"]) not in existing_page_ids and recipe.name.casefold() not in existing_by_name
    ]
    units = {ingredient.unit for recipe in pending for ingredient in recipe.ingredients if ingredient.unit}
    foods = {ingredient.food for recipe in pending for ingredient in recipe.ingredients}
    category_names = {recipe.category for recipe in pending}
    category_names.update(recipe.cuisine for recipe in pending if recipe.cuisine)
    unit_items = ensure_units(api, units) if units else {}
    food_items = ensure_foods(api, foods) if foods else {}
    category_items = ensure_named_items(api, "/api/organizers/categories", category_names) if category_names else {}
    tag_items = ensure_named_items(api, "/api/organizers/tags", {SOURCE_TAG})

    created = 0
    skipped_existing = 0
    skipped_name_conflicts = 0
    for index, recipe in enumerate(selected, 1):
        page_id = str(recipe.source["pageId"])
        if page_id in existing_page_ids:
            skipped_existing += 1
            continue
        if recipe.name.casefold() in existing_by_name:
            skipped_name_conflicts += 1
            continue
        slug = api.request("POST", "/api/recipes", {"name": recipe.name})
        try:
            saved = api.request(
                "PUT",
                f"/api/recipes/{urllib.parse.quote(slug)}",
                recipe_payload(recipe, food_items, unit_items, category_items, tag_items),
            )
        except Exception:
            api.request("DELETE", f"/api/recipes/{urllib.parse.quote(slug)}")
            raise
        existing_by_name[recipe.name.casefold()] = saved
        existing_page_ids.add(page_id)
        created += 1
        if index % 25 == 0:
            sys.stderr.write(f"Imported {index}/{len(selected)} Wikibooks recipes\n")
            sys.stderr.flush()
    return {
        "sourceRecipes": len(selected),
        "createdRecipes": created,
        "skippedExisting": skipped_existing,
        "skippedNameConflicts": skipped_name_conflicts,
        "structuredFoods": len(foods),
        "units": len(units),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:9001")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data, recipes = load_snapshot(args.dataset)
    if args.dry_run:
        sys.stdout.write(
            json.dumps(
                {
                    "sourcePages": data["sourcePages"],
                    "licensedImageRecipes": len(data["recipes"]),
                    "importableRecipes": len(recipes),
                    "license": data["dataLicense"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0
    if args.token_file is None:
        raise ValueError("--token-file is required unless --dry-run is used")
    token = args.token_file.read_text().strip()
    if not token:
        raise ValueError("Token file is empty")
    result = import_recipes(MealieApi(args.base_url, token), recipes, args.limit)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
