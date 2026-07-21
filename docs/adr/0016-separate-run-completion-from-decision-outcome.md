# Separate run completion from decision outcome

The current status contract marks a run completed while retaining a report-writing phase and does not distinguish a published Trading Decision from a non-directional Analysis Outcome. Run Lifecycle Status, Terminal Outcome Kind, and Evidence Integrity Status will be independent fields shared by CLI and programmatic execution. Completion alone can never authorize a signal or memory write, a completed Analysis Outcome is not an operational failure, terminal runs have no active phase, and operational failures carry their own typed error category.
