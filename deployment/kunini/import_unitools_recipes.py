#!/usr/bin/env python3
"""Import the pinned Russian UniTools recipe dataset into Mealie."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DATASET_SHA256 = "41fdc0572b7272ca557d2df22a0fac1cc1f18146e97ebd2c8258c395daf87d8a"
DATASET_COMMIT = "2f554073f7e730b18d707fe1164d18308f6ec16c"
DATASET_PAGE = "https://theunitools.com/ru/data"
ATTRIBUTION = "UniTools — theunitools.com"

CATEGORIES = {
    "main": "Основные блюда",
    "soup": "Супы",
    "dessert": "Десерты",
    "snack": "Закуски",
    "breakfast": "Завтраки",
    "bread": "Выпечка и хлеб",
    "side": "Гарниры",
    "salad": "Салаты",
    "sauce": "Соусы",
    "drink": "Напитки",
}
DIETS = {
    "vegetarian": "Вегетарианское",
    "vegan": "Веганское",
    "gluten-free": "Без глютена",
    "pescatarian": "Пескетарианское",
}
DIFFICULTY = {"easy": "Легко", "medium": "Средне", "hard": "Сложно"}
UNITS = {
    "g": ("грамм", "г", False),
    "kg": ("килограмм", "кг", False),
    "ml": ("миллилитр", "мл", False),
    "l": ("литр", "л", False),
    "piece": ("штука", "шт.", True),
    "tbsp": ("столовая ложка", "ст. л.", True),
    "tsp": ("чайная ложка", "ч. л.", True),
    "toTaste": ("по вкусу", "", True),
    "clove": ("зубчик", "зуб.", True),
    "pinch": ("щепотка", "", True),
    "sprig": ("веточка", "", True),
    "slice": ("ломтик", "", True),
}


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
            query = urllib.parse.urlencode({"page": page, "perPage": 100})
            response = self.request("GET", f"{path}?{query}")
            items.extend(response["items"])
            if page >= response["total_pages"]:
                return items
            page += 1


def load_dataset(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DATASET_SHA256:
        raise ValueError(f"Unexpected dataset SHA-256: {digest}")
    data = json.loads(raw)
    if data.get("version") != "2.0.0" or data.get("license") != "CC BY-SA 4.0":
        raise ValueError("Expected UniTools 2.0.0 under CC BY-SA 4.0")
    recipes = data.get("recipes")
    if not isinstance(recipes, list) or len(recipes) != data.get("counts", {}).get("recipes"):
        raise ValueError("Recipe count does not match dataset metadata")
    return data


def food_catalog(recipes: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    variants: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for recipe in recipes:
        for ingredient in recipe["ingredients"]:
            variants[ingredient["id"]][ingredient["name"]["ru"].strip()] += 1

    canonical_by_source: dict[str, str] = {}
    for source_id, names in variants.items():
        canonical_by_source[source_id] = sorted(
            names.items(), key=lambda item: (-item[1], len(item[0]), item[0].casefold())
        )[0][0]

    catalog: dict[str, dict[str, Any]] = {}
    for source_id, name in canonical_by_source.items():
        key = name.casefold()
        entry = catalog.setdefault(key, {"name": name, "sourceIds": []})
        entry["sourceIds"].append(source_id)
    for entry in catalog.values():
        entry["sourceIds"].sort()
    return catalog, canonical_by_source


def note_for_ingredient(ingredient: dict[str, Any], canonical_name: str) -> str:
    parts: list[str] = []
    original_name = ingredient["name"]["ru"].strip()
    if original_name.casefold() != canonical_name.casefold():
        parts.append(f"В рецепте: {original_name}.")
    note = ingredient.get("note")
    if isinstance(note, dict) and note.get("ru"):
        parts.append(str(note["ru"]).strip())
    scaling = ingredient.get("scaling")
    if scaling == "fixed":
        parts.append("Количество не масштабировать.")
    elif scaling == "damped":
        parts.append("При изменении порций увеличивать умеренно.")
    return " ".join(parts)


def recipe_payload(
    recipe: dict[str, Any],
    data: dict[str, Any],
    countries: dict[str, dict[str, Any]],
    food_by_source: dict[str, dict[str, Any]],
    canonical_by_source: dict[str, str],
    unit_by_source: dict[str, dict[str, Any]],
    category_by_name: dict[str, dict[str, Any]],
    tag_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    country = countries[recipe["country"]]
    category = CATEGORIES[recipe["category"]]
    category_names = [category, country["cuisine"]["ru"].capitalize()]
    tag_names = [DIFFICULTY[recipe["difficulty"]], *(DIETS[diet] for diet in recipe.get("diets", []))]
    tag_names.append("UniTools CC BY-SA 4.0")
    categories = [category_by_name[name.casefold()] for name in category_names]
    tags = [tag_by_name[name.casefold()] for name in tag_names]

    ingredients = []
    for ingredient in recipe["ingredients"]:
        source_id = ingredient["id"]
        ingredients.append(
            {
                "quantity": ingredient.get("quantity") or 0,
                "food": {"id": food_by_source[source_id]["id"], "name": food_by_source[source_id]["name"]},
                "unit": {
                    "id": unit_by_source[ingredient["unit"]]["id"],
                    "name": unit_by_source[ingredient["unit"]]["name"],
                },
                "note": note_for_ingredient(ingredient, canonical_by_source[source_id]),
            }
        )

    photo = recipe.get("photo") or {}
    nutrition = recipe["nutritionPerServing"]
    return {
        "name": recipe["name"]["ru"],
        "recipeServings": recipe["baseServings"],
        "recipeYieldQuantity": recipe["baseServings"],
        "recipeYield": "порций",
        "prepTimeSeconds": recipe["prepMinutes"] * 60,
        "performTimeSeconds": recipe["cookMinutes"] * 60,
        "totalTimeSeconds": (recipe["prepMinutes"] + recipe["cookMinutes"]) * 60,
        "description": f"{recipe['summary']['ru']}\n\nИсточник: {ATTRIBUTION}. Лицензия: CC BY-SA 4.0.",
        "recipeCategory": categories,
        "tags": tags,
        "orgURL": data["landingPage"],
        "recipeIngredient": ingredients,
        "recipeInstructions": [
            {"text": step["text"]["ru"], "durationSeconds": (step.get("minutes") or 0) * 60 or None}
            for step in recipe["steps"]
        ],
        "nutrition": {
            "calories": f"{nutrition['calories']} ккал",
            "proteinContent": f"{nutrition['protein']} г",
            "fatContent": f"{nutrition['fat']} г",
            "carbohydrateContent": f"{nutrition['carbs']} г",
        },
        "settings": {"showNutrition": True},
        "notes": [
            {
                "title": "Источник и лицензия",
                "text": f"Данные: {ATTRIBUTION}\n\n{DATASET_PAGE}\n\nЛицензия: CC BY-SA 4.0",
            }
        ],
        "extras": {
            "unitoolsSlug": recipe["slug"],
            "unitoolsVersion": data["version"],
            "unitoolsCommit": DATASET_COMMIT,
            "unitoolsCountry": recipe["country"],
            "unitoolsLicense": data["license"],
            "unitoolsPhotoUrl": str(photo.get("url") or ""),
            "unitoolsPhotoAuthor": str(photo.get("author") or ""),
            "unitoolsPhotoLicense": str(photo.get("license") or ""),
        },
    }


def ensure_units(api: MealieApi, source_units: set[str]) -> dict[str, dict[str, Any]]:
    unknown = source_units - UNITS.keys()
    if unknown:
        raise ValueError(f"Unsupported units: {sorted(unknown)}")
    existing = api.all_items("/api/units")
    by_source = {
        item.get("extras", {}).get("unitoolsUnit"): item
        for item in existing
        if item.get("extras", {}).get("unitoolsUnit")
    }
    by_name = {item["name"].casefold(): item for item in existing}
    result: dict[str, dict[str, Any]] = {}
    for source in sorted(source_units):
        name, abbreviation, fraction = UNITS[source]
        item = by_source.get(source) or by_name.get(name.casefold())
        if item is None:
            item = api.request(
                "POST",
                "/api/units",
                {
                    "name": name,
                    "abbreviation": abbreviation,
                    "useAbbreviation": bool(abbreviation),
                    "fraction": fraction,
                    "extras": {"unitoolsUnit": source},
                },
            )
        result[source] = item
    return result


def ensure_foods(
    api: MealieApi, catalog: dict[str, dict[str, Any]], canonical_by_source: dict[str, str]
) -> dict[str, dict[str, Any]]:
    existing = api.all_items("/api/foods")
    by_name = {item["name"].casefold(): item for item in existing}
    result: dict[str, dict[str, Any]] = {}
    for key, food in sorted(catalog.items()):
        item = by_name.get(key)
        if item is None:
            item = api.request(
                "POST",
                "/api/foods",
                {
                    "name": food["name"],
                    "description": f"Нормализованный продукт UniTools: {', '.join(food['sourceIds'])}",
                    "extras": {"unitoolsIngredientIds": ",".join(food["sourceIds"])},
                },
            )
        for source_id in food["sourceIds"]:
            result[source_id] = item
    if set(result) != set(canonical_by_source):
        raise AssertionError("Not every source ingredient was mapped")
    return result


def ensure_named_items(api: MealieApi, path: str, names: set[str]) -> dict[str, dict[str, Any]]:
    existing = api.all_items(path)
    by_name = {item["name"].casefold(): item for item in existing}
    for name in sorted(names, key=str.casefold):
        key = name.casefold()
        if key not in by_name:
            by_name[key] = api.request("POST", path, {"name": name})
    return {name.casefold(): by_name[name.casefold()] for name in names}


def import_recipes(api: MealieApi, data: dict[str, Any], limit: int | None = None) -> dict[str, int]:
    recipes = data["recipes"][:limit]
    catalog, canonical_by_source = food_catalog(data["recipes"])
    source_units = {ingredient["unit"] for recipe in recipes for ingredient in recipe["ingredients"]}
    unit_by_source = ensure_units(api, source_units)
    food_by_source = ensure_foods(api, catalog, canonical_by_source)
    countries = {country["code"]: country for country in data["countries"]}
    category_names = {CATEGORIES[recipe["category"]] for recipe in recipes}
    category_names.update(countries[recipe["country"]]["cuisine"]["ru"].capitalize() for recipe in recipes)
    tag_names = {DIFFICULTY[recipe["difficulty"]] for recipe in recipes}
    tag_names.update(DIETS[diet] for recipe in recipes for diet in recipe.get("diets", []))
    tag_names.add("UniTools CC BY-SA 4.0")
    category_by_name = ensure_named_items(api, "/api/organizers/categories", category_names)
    tag_by_name = ensure_named_items(api, "/api/organizers/tags", tag_names)

    existing = api.all_items("/api/recipes")
    existing_by_name = {item["name"].casefold(): item for item in existing}
    created = 0
    skipped = 0
    for index, recipe in enumerate(recipes, 1):
        key = recipe["name"]["ru"].casefold()
        current = existing_by_name.get(key)
        if current:
            full = api.request("GET", f"/api/recipes/{urllib.parse.quote(current['slug'])}")
            if full.get("extras", {}).get("unitoolsSlug") != recipe["slug"]:
                raise ApiError(f"Recipe name conflicts with non-UniTools data: {recipe['name']['ru']}")
            skipped += 1
            continue

        slug = api.request("POST", "/api/recipes", {"name": recipe["name"]["ru"]})
        try:
            saved = api.request(
                "PUT",
                f"/api/recipes/{urllib.parse.quote(slug)}",
                recipe_payload(
                    recipe,
                    data,
                    countries,
                    food_by_source,
                    canonical_by_source,
                    unit_by_source,
                    category_by_name,
                    tag_by_name,
                ),
            )
        except Exception:
            api.request("DELETE", f"/api/recipes/{urllib.parse.quote(slug)}")
            raise
        existing_by_name[key] = saved
        created += 1
        if index % 25 == 0:
            sys.stderr.write(f"Imported {index}/{len(recipes)} recipes\n")
            sys.stderr.flush()
    return {
        "createdRecipes": created,
        "skippedRecipes": skipped,
        "sourceRecipes": len(recipes),
        "normalizedFoods": len(catalog),
        "units": len(source_units),
        "categories": len(category_names),
        "tags": len(tag_names),
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
    data = load_dataset(args.dataset)
    catalog, _ = food_catalog(data["recipes"])
    if args.dry_run:
        sys.stdout.write(
            json.dumps(
                {
                    "recipes": len(data["recipes"]),
                    "sourceIngredientIds": data["counts"]["ingredients"],
                    "normalizedFoods": len(catalog),
                    "license": data["license"],
                    "version": data["version"],
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
    result = import_recipes(MealieApi(args.base_url, token), data, args.limit)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
