import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(MODULE_DIR))

import fetch_wikibooks_recipes as fetcher  # noqa: E402
import import_wikibooks_recipes as importer  # noqa: E402
import sync_recipe_images as images  # noqa: E402


class WikibooksFetcherTest(unittest.TestCase):
    def test_normalize_image_name_handles_wiki_markup(self):
        self.assertEqual(
            fetcher.normalize_image_name("[[Изображение:Baba_Ganoush.jpg|300px]]"),
            "Baba Ganoush.jpg",
        )

    def test_image_license_rejects_noncommercial_and_accepts_sharealike(self):
        self.assertTrue(fetcher.is_free_license("CC BY-SA 4.0"))
        self.assertFalse(fetcher.is_free_license("CC BY-NC 4.0"))


class WikibooksImporterTest(unittest.TestCase):
    def test_parse_ingredient_separates_quantity_unit_food_and_keeps_source(self):
        parsed = importer.parse_ingredient("2 ч. л. свежего тахини")

        self.assertIsNotNone(parsed)
        assert parsed
        self.assertEqual(parsed.quantity, 2)
        self.assertEqual(parsed.unit, "чайная ложка")
        self.assertEqual(parsed.food, "свежего тахини")
        self.assertEqual(parsed.original, "2 ч. л. свежего тахини")

    def test_parse_recipe_uses_first_structured_pair_of_sections(self):
        source = {
            "pageId": 7,
            "title": "Рецепт:Баба гануш",
            "revisionId": 11,
            "sourceUrl": "https://ru.wikibooks.org/w/index.php?oldid=11",
            "image": {
                "url": "https://upload.wikimedia.org/example.jpg",
                "sourceUrl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                "author": "Автор",
                "license": "CC BY-SA 4.0",
                "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            "wikitext": """{{Рецепт
| Категория = Закуски
| Кухня = Арабская кухня
| Порций = 4
| Время = 1 час 15 минут
}}
'''Баба гануш''' — закуска из баклажанов.
=== Состав ===
* 2 баклажана
* 2 ч. л. тахини
=== Приготовление ===
# Запечь баклажаны.
# Смешать с тахини.
== См. также ==
* Хумус
""",
        }

        parsed = importer.parse_recipe(source)

        self.assertIsNotNone(parsed)
        assert parsed
        self.assertEqual(parsed.name, "Баба гануш")
        self.assertEqual(parsed.category, "Закуски")
        self.assertEqual(parsed.servings, 4)
        self.assertEqual(parsed.total_time_seconds, 4500)
        self.assertEqual(len(parsed.ingredients), 2)
        self.assertEqual(parsed.instructions, ["Запечь баклажаны", "Смешать с тахини"])

    def test_import_treats_normalized_slug_collision_as_name_conflict(self):
        class FakeApi:
            def __init__(self):
                self.deleted = []

            def all_items(self, _path):
                return []

            def request(self, method, path, payload=None):
                if method == "POST" and path == "/api/recipes":
                    return "conflict-1"
                if method == "PUT" and path.startswith("/api/recipes/"):
                    raise importer.ApiError("Recipe already exists")
                if method == "DELETE":
                    self.deleted.append(path)
                    return {}
                if method == "POST":
                    return {"id": path, "name": payload["name"]}
                raise AssertionError((method, path, payload))

        recipe = importer.ParsedRecipe(
            source={
                "pageId": 1,
                "revisionId": 2,
                "sourceUrl": "https://ru.wikibooks.org/w/index.php?oldid=2",
                "image": {
                    "url": "https://upload.wikimedia.org/example.jpg",
                    "sourceUrl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                    "author": "Автор",
                    "license": "CC BY-SA 4.0",
                    "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
                },
            },
            name="Наси горенг",
            description="",
            category="Вторые блюда",
            cuisine="",
            servings=2,
            total_time_seconds=None,
            ingredients=[
                importer.ParsedIngredient("рис", 1, "стакан", "1 стакан риса"),
                importer.ParsedIngredient("вода", 2, "стакан", "2 стакана воды"),
            ],
            instructions=["Приготовить"],
        )
        api = FakeApi()

        result = importer.import_recipes(api, [recipe])

        self.assertEqual(result["createdRecipes"], 0)
        self.assertEqual(result["skippedNameConflicts"], 1)
        self.assertEqual(api.deleted, ["/api/recipes/conflict-1"])


class RecipeImageTest(unittest.TestCase):
    def test_choose_source_prefers_explicit_unitools_photo(self):
        source = images.choose_source(
            {"name": "Борщ", "extras": {"unitoolsSlug": "borscht"}},
            {
                "borscht": {
                    "name": {"ru": "Борщ", "en": "Borscht"},
                    "photo": {
                        "url": "https://theunitools.com/recipes/borscht.jpg",
                        "author": "A",
                        "license": "CC BY 2.0",
                    },
                }
            },
            {},
            {},
            images.CommonsResolver(),
            False,
        )

        self.assertIsNotNone(source)
        assert source
        self.assertEqual(source.origin, "UniTools / Wikimedia Commons")
        self.assertEqual(source.license, "CC BY 2.0")

    def test_download_rejects_unapproved_hosts_before_network(self):
        source = images.ImageSource("https://example.com/image.jpg", "", "", "CC0", "", "test")

        with self.assertRaisesRegex(ValueError, "not allowed"):
            images.download_image(source)

    def test_official_wikimedia_thumbnail_host_is_allowed(self):
        self.assertIn("thumb.wikimedia.org", images.ALLOWED_IMAGE_HOSTS)


if __name__ == "__main__":
    unittest.main()
