import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "import_unitools_recipes.py"
SPEC = importlib.util.spec_from_file_location("import_unitools_recipes", MODULE_PATH)
assert SPEC and SPEC.loader
IMPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORTER)


class ImporterTest(unittest.TestCase):
    def test_food_catalog_deduplicates_source_ids_with_the_same_canonical_name(self):
        recipes = [
            {
                "ingredients": [
                    {"id": "tomato", "name": {"ru": "Помидоры"}},
                    {"id": "tomato", "name": {"ru": "Томаты"}},
                    {"id": "tomatoes", "name": {"ru": "Помидоры"}},
                ]
            },
            {"ingredients": [{"id": "tomato", "name": {"ru": "Помидоры"}}]},
        ]

        catalog, canonical = IMPORTER.food_catalog(recipes)

        self.assertEqual(canonical, {"tomato": "Помидоры", "tomatoes": "Помидоры"})
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog["помидоры"]["sourceIds"], ["tomato", "tomatoes"])

    def test_ingredient_note_preserves_variant_and_non_linear_scaling(self):
        ingredient = {
            "name": {"ru": "Говяжья вырезка"},
            "note": {"ru": "нарезать полосками"},
            "scaling": "damped",
        }

        note = IMPORTER.note_for_ingredient(ingredient, "Говядина")

        self.assertIn("В рецепте: Говяжья вырезка.", note)
        self.assertIn("нарезать полосками", note)
        self.assertIn("увеличивать умеренно", note)


if __name__ == "__main__":
    unittest.main()
