"""Pure configuration tests; run only through the task's isolated bootstrap."""
import ast
import copy
import json
import os
import runpy
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
PROFILE = {
    "market_history_mode": "current",
    "data_usage_mode": "personal_research",
    "mainland_capability_routing_mode": "legacy",
    "market_data_vendors": {"cn_a": {"core_stock_apis": "tushare"}},
}


@contextmanager
def synthetic_environment(values):
    """Only called after the bootstrap has discarded inherited environment."""
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


@unittest.skipUnless(
    os.environ.get("MARKET_CONFIG_ISOLATED") == "1", "requires isolated bootstrap"
)
class LocalRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="case-", dir=os.environ["MARKET_CONFIG_TEST_ROOT"]
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installation = self.root / "installation"
        self.module = self.installation / "tradingagents" / "default_config.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_bytes((SOURCE / "tradingagents/default_config.py").read_bytes())
        checksum = self.module.parent / "config" / "crypto_identity_registry.sha256"
        checksum.parent.mkdir()
        checksum.write_text("0" * 64 + "  registry.json\n", encoding="utf-8")
        self.project = self.root / "launcher-project"
        self.project.mkdir()
        self.base_env = {
            "HOME": str(self.root),
            "USERPROFILE": str(self.root),
            "TEMP": str(self.root),
            "TMP": str(self.root),
        }

    def load(self, env=None, launcher=True):
        environment = dict(self.base_env)
        if launcher:
            environment["TRADINGAGENTS_PROJECT_ROOT"] = str(self.project)
        environment.update(env or {})
        with synthetic_environment(environment):
            return runpy.run_path(str(self.module))

    def profile(self, value, root=None):
        (root or self.project).joinpath("tradingagents.local.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_absent_profile_preserves_defaults_and_limits(self):
        config = self.load()["DEFAULT_CONFIG"]
        self.assertEqual(config["market_history_mode"], "shadow")
        self.assertEqual(
            config["market_data_vendors"]["cn_a"]["core_stock_apis"],
            "akshare,baostock,yfinance",
        )
        self.assertEqual(config["tushare_calls_per_minute"], 40)
        self.assertEqual(config["tushare_operator_safety_ceiling_calls_per_minute"], 40)
        self.assertNotIn("market_history_current_cache_max_age_seconds", config)

    def test_absent_and_empty_vendor_env_preserve_existing_nested_settings(self):
        module = self.load()
        original = {"market_data_vendors": {
            "cn_a": {"core_stock_apis": "baostock", "news_data": "existing-news"},
            "us": {"core_stock_apis": "existing-us"},
        }}
        for env in ({}, {"TRADINGAGENTS_CN_A_CORE_STOCK_APIS": ""}):
            with self.subTest(env=env), synthetic_environment(env):
                value = copy.deepcopy(original)
                self.assertEqual(module["_apply_env_overrides"](value), original)

    def test_vendor_env_changes_only_cn_a_core_and_does_not_mutate_aliases(self):
        module = self.load()
        vendors = {"cn_a": {"core_stock_apis": "baostock", "news_data": "news"},
                   "us": {"core_stock_apis": "us"}}
        old = copy.deepcopy(vendors)
        config = {"market_data_vendors": vendors, "other": 12}
        with synthetic_environment({"TRADINGAGENTS_CN_A_CORE_STOCK_APIS": "tushare"}):
            module["_apply_env_overrides"](config)
        self.assertEqual(vendors, old)
        self.assertEqual(config["market_data_vendors"]["cn_a"]["core_stock_apis"], "tushare")
        self.assertEqual(config["market_data_vendors"]["cn_a"]["news_data"], "news")
        self.assertEqual(config["market_data_vendors"]["us"], old["us"])
        self.assertEqual(config["other"], 12)

    def test_launcher_profile_overrides_conflicting_environment_only_for_four_fields(self):
        self.profile(PROFILE)
        env = {
            "TRADINGAGENTS_MARKET_HISTORY_MODE": "shadow",
            "TRADINGAGENTS_DATA_USAGE_MODE": "production",
            "TRADINGAGENTS_MAINLAND_CAPABILITY_ROUTING_MODE": "qualified",
            "TRADINGAGENTS_CN_A_CORE_STOCK_APIS": "baostock",
            "TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese",
        }
        config = self.load(env)["DEFAULT_CONFIG"]
        self.assertEqual(config["market_history_mode"], "current")
        self.assertEqual(config["data_usage_mode"], "personal_research")
        self.assertEqual(config["mainland_capability_routing_mode"], "legacy")
        self.assertEqual(config["market_data_vendors"]["cn_a"]["core_stock_apis"], "tushare")
        self.assertEqual(config["market_data_vendors"]["cn_a"]["news_data"], "akshare,yfinance")
        self.assertEqual(config["output_language"], "Chinese")
        self.assertEqual(config["tushare_calls_per_minute"], 40)

    def test_direct_cli_uses_installation_root_not_cwd(self):
        self.profile(PROFILE, self.installation)
        config = self.load(launcher=False)["DEFAULT_CONFIG"]
        self.assertEqual(config["market_history_mode"], "current")
        self.assertEqual(config["market_data_vendors"]["cn_a"]["core_stock_apis"], "tushare")

    def test_launcher_root_takes_precedence_over_installation_root(self):
        self.profile(PROFILE, self.installation)
        self.profile({"market_history_mode": "shadow"})
        self.assertEqual(self.load()["DEFAULT_CONFIG"]["market_history_mode"], "shadow")

    def test_partial_profile_preserves_environment_and_nested_categories(self):
        self.profile({"market_data_vendors": PROFILE["market_data_vendors"]})
        config = self.load({"TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese"})["DEFAULT_CONFIG"]
        self.assertEqual(config["market_history_mode"], "shadow")
        self.assertEqual(config["output_language"], "Chinese")
        self.assertEqual(config["market_data_vendors"]["cn_a"]["news_data"], "akshare,yfinance")

    def test_invalid_or_unrelated_profile_fields_are_rejected(self):
        invalid = [
            [],
            {"unexpected": "synthetic"},
            {"market_history_mode": "authoritative"},
            {"market_history_mode": []},
            {"data_usage_mode": "production"},
            {"mainland_capability_routing_mode": "qualified"},
            {"market_data_vendors": {"us": {"core_stock_apis": "tushare"}}},
            {"market_data_vendors": {"cn_a": {"news_data": "tushare"}}},
            {"market_data_vendors": {"cn_a": {"core_stock_apis": ""}}},
            {"market_data_vendors": {"cn_a": {"core_stock_apis": 2}}},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.profile(value)
                with self.assertRaises(ValueError):
                    self.load()

    def test_duplicate_and_malformed_json_are_rejected(self):
        path = self.project / "tradingagents.local.json"
        for text in (
            '{"market_history_mode":"shadow","market_history_mode":"current"}',
            '{"market_data_vendors":{"cn_a":{"core_stock_apis":"tushare","core_stock_apis":"baostock"}}}',
            "{",
        ):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.load()

    def test_real_cli_config_builder_keeps_loaded_profile(self):
        self.profile(PROFILE)
        config = self.load()["DEFAULT_CONFIG"]
        tree = ast.parse((SOURCE / "cli/main.py").read_text(encoding="utf-8"))
        builder = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_build_run_config"
        )
        namespace = {"DEFAULT_CONFIG": config, "os": os}
        exec(compile(ast.Module(body=[builder], type_ignores=[]), "<cli-config-builder>", "exec"), namespace)
        selections = {
            "research_depth": 1, "shallow_thinker": "synthetic",
            "deep_thinker": "synthetic", "backend_url": None, "llm_provider": "openai",
        }
        with synthetic_environment({}):
            result = namespace["_build_run_config"](selections, None)
        for key in PROFILE:
            self.assertEqual(result[key], config[key])
        self.assertEqual(result["market_data_vendors"]["cn_a"]["core_stock_apis"], "tushare")
