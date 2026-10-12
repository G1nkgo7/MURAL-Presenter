# Third-party notices

MuralPresenter project code is distributed under the [MIT License](../LICENSE).
Third-party components, dependencies and fonts retain their own licenses.

## Bundled components

- **Apache ECharts 5.5.0:** `skills/mural-presenter-v1/assets/vendor/echarts.min.js`.
  The original header is retained. See the [upstream license](../LICENSES/Apache-ECharts.txt),
  [NOTICE](../LICENSES/Apache-ECharts-NOTICE.txt) and [d3 subcomponent license](../LICENSES/Apache-ECharts-d3.txt),
  copied from the [5.5.0 source release](https://github.com/apache/echarts/tree/5.5.0).
- **Presentation and UI fonts:** files under `fonts/` and `webui/studio/static/fonts/`
  use the [SIL Open Font License 1.1](../LICENSES/OFL-1.1.txt).
  Their [copyright notices](../LICENSES/font-notices.md) and original embedded metadata are retained.
  The Noto Sans SC WOFF2 subsets retain full character coverage and their source license;
  `studio/static/fonts/ui-cjk.json` records the source hash and generated files.

## Installed dependencies and generated content

Python dependencies are declared in `studio/pyproject.toml` and `inference/pyproject.toml`.
Their own license texts and metadata are available from the corresponding upstream projects
and installed distributions; the project MIT license does not replace those terms.

Generated presentations may incorporate user-supplied or remotely retrieved material.
Such material remains subject to its original rights and permissions, rather than acquiring
the project license automatically.
