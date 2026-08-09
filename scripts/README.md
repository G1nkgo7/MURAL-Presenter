# Command entrypoints

Future commands for query generation, data preparation, inference, QC, evaluation, and export will
live here as thin wrappers. A script may parse arguments, load a named configuration, invoke an
importable module, and report the resulting manifest or run ID; reusable business logic belongs in
`src/mural_presenter/`.

Every command should support a dry-run or validation mode where meaningful, fail before model calls on
invalid configuration, and print the exact output manifest path.
