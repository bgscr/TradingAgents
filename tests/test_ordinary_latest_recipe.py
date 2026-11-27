"""Run the unchanged public PowerShell scripts against a transport-free synthetic latest worker."""

import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
P = runpy.run_path(str(ROOT / "scripts/propose_ordinary_latest_recipe.py"))
H = P["H"]
DAY = "2026-11-26"
DAILY = "https://api.waditu.com/dataapi/daily"


def write(root, name, data):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def document(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fixture_close():
    observed = DAY + "T16:05:01+08:00"
    fields = ["ts_code", "trade_date", "close"]
    body = {
        "code": 0,
        "msg": "",
        "data": {"fields": fields, "items": [["601328.SH", "20261126", 4.0]]},
    }
    raw = H["sha"](document(body))
    receipt = {
        "status": "observed_daily_close",
        "http_status": 200,
        "source": DAILY,
        "response_sha256": raw,
        "scope": "auction_close_price",
        "ticker": "601328.SH",
        "trading_date": DAY,
        "published_at": None,
        "observed_at": observed,
        "first_observed_at": observed,
        "close": 4.0,
    }
    reservation = {
        "method": "POST",
        "url": DAILY,
        "fields": fields,
        "reserved": 1,
        "params": {
            "ts_code": "601328.SH",
            "trade_date": "20261126",
            "ts_type_name": "https://api.waditu.com/dataapi",
        },
        "started_at": DAY + "T16:05:00+08:00",
    }
    contract = {
        "adapter": "tushare-daily-close-v1",
        "source": DAILY,
        "scope": "auction_close_price",
    }
    originals = {H["sha"](document(v)): document(v) for v in (body, receipt, reservation, contract)}
    row = {
        "trading_date": DAY,
        "raw_sha256": raw,
        "observed_at": observed,
        "published_at": None,
        "reservation_sha256": H["sha"](document(reservation)),
        "receipt_sha256": H["sha"](document(receipt)),
    }
    projection = document(
        {
            "ticker": "601328.SS",
            "source": DAILY,
            "contract_sha256": H["sha"](document(contract)),
            "rows": [row],
        }
    )
    return projection, originals


class ProposalContract(unittest.TestCase):
    def test_old_recipe_and_runtime_are_preserved_and_public_changes_are_exact(self):
        old = H["recipe"](ROOT)
        candidate = P["proposal"]()
        old_public = H["public"](old)
        self.assertEqual(candidate["status"], "unapproved")
        self.assertEqual(
            set(old_public) | set(P["added_sources"]()), set(candidate["public_project"])
        )
        self.assertEqual(len(old_public), 301)
        self.assertEqual(
            {n for n in old_public if old_public[n] != candidate["public_project"][n]}, P["CHANGED"]
        )
        self.assertEqual(candidate["target_runtime"], old["runtime"])
        self.assertEqual(
            candidate["explicit_historical_replay"]["paths"], old["local_evidence"]["paths"]
        )
        self.assertEqual(
            candidate["explicit_historical_replay"]["requires_parent_recipe"], H["PIN"]
        )

    def test_wrong_date_unreviewed_digest_and_other_source_do_not_propose_bindings(self):
        projection, _ = fixture_close()
        for ticker, day, raw, digest, reason in (
            ("601328.SS", DAY, projection, "0" * 64, "projection_digest_mismatch"),
            ("601328.SS", "2026-11-25", projection, H["sha"](projection), "wrong_close_date"),
            ("000001.SZ", DAY, projection, H["sha"](projection), "wrong_close_source"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                P["proposed_close_bindings"](ticker, day, raw, digest)


@unittest.skipUnless(
    os.name == "nt", "requires reviewed Windows PowerShell 7 and a local project runtime"
)
class LatestRecipe(unittest.TestCase):
    @staticmethod
    def historical_inputs(key):
        return H["public"](H["recipe"](ROOT)), H["R"]["small_file"](ROOT / key, 500000)

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="latest recipe lab ")
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        cls.public = cls.root / "public"
        cls.prepare_public()
        cls.station = cls.root / "independent fixture inputs"
        cls.pwsh = shutil.which("pwsh.exe")
        policy = json.loads((cls.public / "config/ordinary-full-pair-policy.json").read_bytes())
        if not cls.pwsh or H["sha"](Path(cls.pwsh).read_bytes()) != policy["executable_sha256"]:
            raise ValueError("reviewed_powershell_required")
        cls.deps = Path(sys.executable).absolute().parents[1] / "Lib/site-packages"
        cls.env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP")}
        cls.env.update(POWERSHELL_TELEMETRY_OPTOUT="1", DOTNET_CLI_TELEMETRY_OPTOUT="1")
        cls.projection, cls.originals = fixture_close()
        cls.close_pins = P["proposed_close_bindings"](
            "601328.SS", DAY, cls.projection, H["sha"](cls.projection)
        )
        cls.review_key = "config/full_evidence/XSHG/601328.SS/latest-target-review.json"
        write(cls.root, "SYNTHETIC-ONLY", b"no authority\n")

    @classmethod
    def prepare_public(cls):
        cls.draft = P["proposal"]()
        cls.pins = cls.draft["public_project"]
        # Never read the historical receipt or either private original, even if locally available.
        for name, digest in cls.pins.items():
            if name in cls.draft["proposed_additions"]:
                data = P["added_sources"]()[name]
            else:
                data = H["R"]["small_file"](ROOT / name, 32_000_000)
            if name in P["CHANGED"]:
                before = cls.draft["proposed_changes"][name]["before"]
                if name == "scripts/full_calendar_cache.py":
                    lf = data.replace(b"\r\n", b"\n")
                    old = next(
                        b for b in (data, lf, lf.replace(b"\n", b"\r\n")) if H["sha"](b) == before
                    )
                    write(cls.root / "frozen control", name, old)
                data = P["proposed_source"](name, data, before)
            lf = data.replace(b"\r\n", b"\n")
            chosen = next(
                (b for b in (data, lf, lf.replace(b"\n", b"\r\n")) if H["sha"](b) == digest), None
            )
            if chosen is None and name in {
                "tradingagents/dataflows/current_statement_cache.py",
                "tradingagents/dataflows/current_statement_diagnostics.py",
            }:
                # A clean checkout after Issue #7 has the newer pair. The v1
                # control must still exercise the pinned historical file bytes.
                raw = H["git"](ROOT, ["cat-file", "blob", H["RELEASE"] + ":" + name])
                lf = raw.replace(b"\r\n", b"\n")
                chosen = next(
                    (b for b in (raw, lf, lf.replace(b"\n", b"\r\n")) if H["sha"](b) == digest),
                    None,
                )
            if chosen is None:
                raise ValueError("use_clean_reviewed_snapshot: " + name)
            write(cls.public, name, chosen)

    def setUp(self):
        self.site = self.root / self._testMethodName
        for n in (
            "start_tradingagents.ps1",
            "scripts/Start-MainlandAnalysis.ps1",
            "scripts/full_date_evidence.py",
        ):
            write(self.site, n, (self.public / n).read_bytes())
        venv.EnvBuilder(with_pip=False).create(self.site / ".venv")
        for role, name in (
            ("seed", "seed.py"),
            ("reader", "scripts/full_calendar_cache.py"),
            ("entry", "scripts/analyze_mainland_bounded.py"),
        ):
            if role == "reader" and self._testMethodName == "test_frozen_reader_import_control":
                role = "reader_parent_control"
            args = tuple(str(p) for p in (self.site, self.public, self.deps, self.station)) + (
                role,
            )
            lab = str(ROOT / "tests/ordinary_latest_recipe_fixture.py")
            write(
                self.site,
                name,
                f"import runpy\nraise SystemExit(runpy.run_path({lab!r})['worker'](*{args!r}))\n".encode(),
            )
        for n, digest in self.close_pins.items():
            write(self.site, n, self.originals.get(digest, self.projection))

    def seed(self, venue="SSE"):
        command = [sys.executable, "-I", "-S", "-B", str(self.site / "seed.py"), venue]
        p = subprocess.run(command, cwd=self.site, env=self.env, capture_output=True, timeout=45)
        self.assertEqual(p.returncode, 0, p.stderr.decode(errors="replace")[-1400:])
        row = json.loads(self.projection)["rows"][0]
        claim = {
            "contract": "ordinary-latest-target-review-v1",
            "purpose": "current_retrospective_selection",
            "projection_sha256": H["sha"](self.projection),
            "contract_sha256": json.loads(self.projection)["contract_sha256"],
            "security": "601328.SS",
            "venue": "XSHG",
            "trading_date": DAY,
            "calendar_revision": json.loads(p.stdout),
            "calendar_observed_at": DAY + "T17:00:00+08:00",
            **{k: row[k] for k in P["DAILY_REFS"][1:]},
        }
        self.review(claim)

    def review(self, claim):
        raw = document(claim)
        write(self.site, self.review_key, raw)
        write(
            self.station,
            self.site.name + "-pins.json",
            document(self.pins | self.close_pins | {self.review_key: H["sha"](raw)}),
        )

    def launch(self, *options):
        command = [
            self.pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(self.site / "start_tradingagents.ps1"),
            "-Full",
            "-Ticker",
            "601328.SH",
            "-CheckOnly",
            *options,
        ]
        p = subprocess.run(
            command, cwd=self.site.parent, env=self.env, capture_output=True, timeout=45
        )
        self.assertFalse((self.site / "config/bounded_mainland_release.json").exists())
        self.assertFalse((self.site / "reports").exists())
        self.assertTrue(p.stdout.strip(), p.stderr.decode(errors="replace")[-1000:])
        for guard in self.site.glob("fixture-guard-*.jsonl"):
            for line in guard.read_bytes().splitlines():
                self.assertEqual(json.loads(line)["unexpected"], 0, (guard.name, json.loads(line)))
        return p.returncode, json.loads(p.stdout)

    def test_normal_latest(self):
        self.seed()
        code, data = self.launch()
        self.assertEqual(code, 0, data)
        self.assertEqual(data["status"], "selection_verified")
        selected = data["selection"]
        self.assertEqual(
            (selected["security"], selected["venue"], selected["trading_date"]),
            ("601328.SS", "XSHG", DAY),
        )
        self.assertFalse(selected["counts_as_full_acceptance"])
        self.assertIsNone(data["ready_for_live"])
        for role, children in (("entry", 1), ("reader", 0), ("seed", 0)):
            audit = json.loads((self.site / ("fixture-guard-" + role + ".jsonl")).read_bytes())
            self.assertEqual(audit["permitted_reader_children"], children)
            self.assertEqual(audit["unexpected"], 0)

    def test_frozen_reader_import_control(self):
        self.seed()
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "missing_module_full_auction_schedule")

    def test_missing_and_wrong_exchange(self):
        self.seed("SZSE")
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "calendar_coverage_missing")

    def test_local_calendar_missing(self):
        self.seed("missing")
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "calendar_coverage_missing")

    def test_mixed_store_binds_exchange_revision_and_observation(self):
        self.seed("mixed")
        code, data = self.launch()
        self.assertEqual(code, 0, data)
        selected = data["selection"]
        expected = json.loads((self.site / self.review_key).read_bytes())
        self.assertEqual(selected["calendar_revision"], expected["calendar_revision"])

    def test_mixed_store_conflict_remains_a_veto(self):
        self.seed("conflict")
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "calendar_membership_conflict")

    def test_independent_calendar_revision_must_match(self):
        self.seed()
        expected = json.loads((self.site / self.review_key).read_bytes())
        self.review(expected | {"calendar_revision": "different-training-revision"})
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "target_evidence_mismatch_calendar_revision")

    def test_target_cannot_introduce_its_own_review_or_claim_historical_purpose(self):
        self.seed()
        expected = json.loads((self.site / self.review_key).read_bytes())
        write(self.station, self.site.name + "-pins.json", document(self.pins | self.close_pins))
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "reviewed_input_missing")
        self.review(expected | {"purpose": "as_known_on_date"})
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "wrong_evidence_purpose")

    def test_missing_close_and_tampered_original(self):
        self.seed()
        name = "config/full_evidence/market-releases.json"
        (self.site / name).unlink()
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "FileNotFoundError")
        write(self.site, name, self.projection)
        sha = json.loads(self.projection)["rows"][0]["raw_sha256"]
        write(
            self.site, "config/full_evidence/originals/" + sha + ".bin", b"changed synthetic close"
        )
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "security_close_missing")

    def test_wrong_expected_day_and_historical_is_separate(self):
        self.seed()
        code, data = self.launch("-ExpectedTradingDate", "2026-11-25")
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "unexpected_trading_date")
        code, data = self.launch("-AnalysisDate", "2026-11-20")
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "reviewed_input_missing")

    def test_proposal_cannot_start_formal_entry(self):
        entry = (self.public / "scripts/analyze_mainland_bounded.py").read_bytes()
        write(self.site, "scripts/analyze_mainland_bounded.py", entry)
        write(self.site, "unapproved-proposal.json", document(self.draft))
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["status"], "stopped")
        self.assertEqual(data["reason"], "missing_bound_file")
        verify = runpy.run_path(str(self.public / "scripts/analyze_mainland_bounded.py"))[
            "verify_release"
        ]
        with self.assertRaisesRegex(RuntimeError, "release_contract"):
            verify(self.draft, source=self.site)

    def test_research_activation_requires_the_exact_reviewed_intake_postimage(self):
        script = "scripts/analyze_mainland_bounded.py"
        runner = write(self.site, script, (self.public / script).read_bytes())
        verify = runpy.run_path(str(runner))["research_policy"]
        key = P["ACTIVATION"]
        after = (self.public / key).read_bytes()
        before = after.replace(P["INTAKE_AFTER"].encode(), P["INTAKE_BEFORE"].encode())
        self.assertEqual(H["sha"](before), self.draft["proposed_changes"][key]["before"])
        policy = write(self.site, key, before)
        pins = dict(self.pins)
        pins[key] = H["sha"](before)

        def index():
            return {
                "files": [{"root": "project", "relative": n, "sha256": v} for n, v in pins.items()]
            }

        with self.assertRaisesRegex(RuntimeError, "research_activation_unbound"):
            verify(index())
        policy.write_bytes(after)
        with self.assertRaisesRegex(RuntimeError, "research_activation_changed"):
            verify(index())
        pins[key] = H["sha"](after)
        accepted = verify(index())
        self.assertEqual(accepted["mode"], "portfolio-research-v3")
        self.assertEqual(accepted["budget"], [60, 3, 1200])
        self.assertIs(accepted["search_enabled"], False)
        self.assertEqual(accepted["sources"][P["INTAKE"]], self.pins[P["INTAKE"]])

    def test_historical_resolver_with_its_own_synthetic_inputs(self):
        # Independently provision a different, training-only historical profile. No latest
        # card, target calendar store, or original release's private evidence is available.
        day = "2026-11-19"
        cal = {
            "normalizer_version": "tushare-market-history-normalizer-v1",
            "adjustment_methodology": "tushare-adj-factor-divide-end-anchor-qfq-v1",
            "calendar": [{"cal_date": "20261119", "exchange": "SSE", "is_open": "1"}],
        }
        raw_hash = H["sha"](document(cal))
        fields = {
            "payload_digest": raw_hash,
            "provider_dataset_id": "synthetic-training-calendar",
            "reference_market": "mainland-cn",
            "timezone_name": "Asia/Shanghai",
            "sessions": [
                {"session_date": day, "status": "open", "opens_at": None, "closes_at": None}
            ],
        }
        revision = "market-session-calendar=sha256:" + H["sha"](
            document({"fields": fields, "kind": "market-session-calendar"})
        )
        handoff = {
            "context": {
                "venue": "XSHG",
                "destination": "https://api.waditu.com/dataapi/trade_cal",
                "analysis_date": day,
                "refresh_source": fields["provider_dataset_id"],
            },
            "result": {"status": "ready", "revision": revision, "analysis_date": day},
        }
        receipt = {
            "handoff_sha256": H["sha"](document(handoff)),
            "raw_sha256": raw_hash,
            "venue": "XSHG",
            "day": day,
            "revision": revision,
            "observed_at": day + "T17:00:00+08:00",
        }
        key = "scripts/full_date_evidence.py"
        old, original = self.historical_inputs(key)
        original = original.replace(b"\r\n", b"\n")
        write(
            self.site,
            key,
            next(
                b for b in (original, original.replace(b"\n", b"\r\n")) if H["sha"](b) == old[key]
            ),
        )
        for name, data in [
            (f"config/full_evidence/XSHG/{day}/completed-day.json", receipt),
            (f"config/full_evidence/originals/{raw_hash}.bin", cal),
            ("config/full_evidence/originals/" + receipt["handoff_sha256"] + ".bin", handoff),
        ]:
            write(self.site, name, document(data))
            old[name] = H["sha"](document(data))
        write(self.station, self.site.name + "-pins.json", document(old))
        code, data = self.launch("-AnalysisDate", day)
        self.assertEqual(code, 0, data)
        self.assertEqual(
            data, {"status": "synthetic_historical_selection", "day": day, "ready_for_live": False}
        )


if __name__ == "__main__":
    unittest.main()
