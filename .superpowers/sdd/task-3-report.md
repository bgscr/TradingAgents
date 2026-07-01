Status: DONE

Commits created:
- fix: make complete reports summary first

Test summary:
- RED: `rtk pytest tests/test_reporting.py::test_complete_report_is_summary_first_and_links_full_histories -q` failed because the portfolio-first heading was missing.
- GREEN: `rtk pytest tests/test_reporting.py -q` passed with 4 tests.

Concerns:
- None.
