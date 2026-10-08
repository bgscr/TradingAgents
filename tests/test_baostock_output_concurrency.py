"""No-network regressions for the BaoStock text-output containment boundary.

The SDK is represented only by thread Events and Mock callbacks. These tests
must not import or invoke BaoStock, spawn subprocesses, or touch persistent data.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from queue import Queue
from unittest.mock import Mock


# Load only this stdlib-only source file. Importing tradingagents.dataflows
# would also execute package initializers outside this unit's reviewed scope.
_SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tradingagents" / "dataflows" / "baostock_output.py"
)
_SPEC = importlib.util.spec_from_file_location("_baostock_output_unit_target", _SOURCE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load the BaoStock output containment source")
_TARGET = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_TARGET)
contain_baostock_output = _TARGET.contain_baostock_output


class BaoStockOutputConcurrencyTests(unittest.TestCase):
    def _thread(self, action, errors: Queue):
        def run():
            try:
                action()
            except BaseException as exc:
                errors.put(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _join(self, threads, errors: Queue):
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "containment worker did not exit")
        if not errors.empty():
            raise errors.get_nowait()

    def test_logout_runs_before_blocked_query_is_released(self):
        out, err = io.StringIO(), io.StringIO()
        entered, release, logged_out = (threading.Event() for _ in range(3))
        errors, threads = Queue(), []

        def query():
            with contain_baostock_output():
                print("query-secret")
                print("query-secret", file=sys.stderr)
                entered.set()
                self.assertTrue(release.wait(5), "query was not released")

        def logout_action():
            print("logout-secret")
            print("logout-secret", file=sys.stderr)
            logged_out.set()

        logout = Mock(side_effect=logout_action)

        def control():
            with contain_baostock_output():
                logout()

        with redirect_stdout(out), redirect_stderr(err):
            try:
                threads.append(self._thread(query, errors))
                self.assertTrue(entered.wait(2))
                threads.append(self._thread(control, errors))
                # Ordinary threads remain visible while both SDK scopes exist.
                print("ordinary-output")
                print("ordinary-error", file=sys.stderr)
                self.assertTrue(logged_out.wait(2), "logout blocked behind query")
            finally:
                release.set()
                self._join(threads, errors)
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
        logout.assert_called_once_with()
        self.assertEqual(out.getvalue(), "ordinary-output\n")
        self.assertEqual(err.getvalue(), "ordinary-error\n")

    def test_overlapping_scopes_can_exit_in_non_lifo_order(self):
        out, err = io.StringIO(), io.StringIO()
        a_entered, b_entered, release_a, a_exited, release_b = (
            threading.Event() for _ in range(5)
        )
        errors, threads = Queue(), []

        def first():
            with contain_baostock_output():
                a_entered.set()
                self.assertTrue(release_a.wait(5))
            a_exited.set()

        def second():
            with contain_baostock_output():
                b_entered.set()
                self.assertTrue(a_exited.wait(5))
                print("still-suppressed")
                print("still-suppressed", file=sys.stderr)
                self.assertTrue(release_b.wait(5))

        with redirect_stdout(out), redirect_stderr(err):
            try:
                threads.append(self._thread(first, errors))
                self.assertTrue(a_entered.wait(2))
                threads.append(self._thread(second, errors))
                self.assertTrue(b_entered.wait(2), "second scope serialized")
                release_a.set()
                self.assertTrue(a_exited.wait(2))
                print("ordinary-between-exits")
            finally:
                release_a.set()
                release_b.set()
                self._join(threads, errors)
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
        self.assertEqual(out.getvalue(), "ordinary-between-exits\n")
        self.assertEqual(err.getvalue(), "")

    def test_nested_scope_exception_does_not_restore_outer_scope_early(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with contain_baostock_output():
                print("outer-secret")
                with self.assertRaisesRegex(ValueError, "sentinel"):
                    with contain_baostock_output():
                        sys.stdout.writelines(["nested-secret", "nested-secret"])
                        sys.stderr.flush()
                        raise ValueError("sentinel")
                print("after-inner-secret")
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
            print("visible-after-exit")
        self.assertEqual(out.getvalue(), "visible-after-exit\n")
        self.assertEqual(err.getvalue(), "")

    def test_cleanup_does_not_overwrite_external_redirect(self):
        out, err, replacement = io.StringIO(), io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            scope = contain_baostock_output()
            scope.__enter__()
            proxy = sys.stdout
            redirect = redirect_stdout(replacement)
            redirect.__enter__()
            try:
                scope.__exit__(None, None, None)
                self.assertIs(sys.stdout, replacement)
                self.assertIs(sys.stderr, err)
            finally:
                redirect.__exit__(None, None, None)
            # An external redirect may restore the retired proxy. It must be
            # transparent; a later scope must not accumulate stale wrappers.
            self.assertIs(sys.stdout, proxy)
            print("visible-through-retired-proxy")
            with contain_baostock_output():
                print("new-secret")
            self.assertIs(sys.stdout, out)
        self.assertEqual(out.getvalue(), "visible-through-retired-proxy\n")
        self.assertEqual(replacement.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
