# Contributing

Use a topic branch and keep changes scoped. The current paper source pair is frozen to the 0919
experimental inventory; do not update its hashes merely to make validation pass.
Deployment and WebUI changes belong outside the frozen pair and require integration tests plus
a refreshed, explicitly named integration manifest. Record what was tested and whether model
requests were made. Keep credentials, datasets, generated workspaces and the separate site local.

Run `python tools/check_paper_release.py` before submitting changes. Follow the launch and
validation commands in README.md. Licensing and final paper metadata remain author decisions.

The root `pytest.ini` selects portable integration tests. The CI workflow explicitly selects
the release/configuration contracts and current WebUI tests as well. Upstream historical tests
may require archived runtimes or external canary assets that are not part of this source package.
See the root README for tested scope and known failures; a passing packaging CI is not a
claim that the complete experimental suite or benchmark has passed.

The public branch contains the runtime, WebUI and their maintenance files. Keep benchmark,
root docs, examples, design drafts, training and paper workspaces local; do not force-add them.
Keep public brand exports in `assets/logo/exports/` and method figures in `assets/figures/`.
Both README editions should retain the project identity and method overview alongside current usage.
The frozen core's own docs, tests and assets remain part of its original inventory.
After reviewing and testing integration changes, commit the source changes, then run
`python tools/freeze_paper_integration.py` and `python tools/check_paper_release.py`.
Commit the resulting manifest separately so its source commit identifies the reviewed source.
