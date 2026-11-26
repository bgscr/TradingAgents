"""Verify the generic recipe without a machine binding, real store, or credentials."""
from pathlib import Path
import importlib.util
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixed_recipe", ROOT/"scripts/derive_fixed_source.py")
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


def test_patch_reconstructs_reviewed_bytes():
    assert recipe.digest(recipe.patch_bytes()) == recipe.PATCH


def test_financial_inventory_pins():
    for name, expected in recipe.FINANCE_INPUTS.items():
        data = subprocess.check_output(["git", "--no-replace-objects", "-C", str(ROOT),
                                        "cat-file", "blob", recipe.REV + ":" + name])
        assert recipe.digest(data) == expected
