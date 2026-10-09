"""用真实 Chromium 验证屏显文字采样，不改写作者的 SVG 或内联 HTML。"""
from playwright.sync_api import sync_playwright
import pytest

from geometry_fixture import geometry, require_browser, write_geometry
from runtime.shared_objects import workspace_skill_module


@pytest.fixture
def sampled_page(tmp_path, monkeypatch):
    write_geometry(tmp_path, geometry(width_px=800, height_px=600))
    renderer = workspace_skill_module(tmp_path, "render_deck")
    for variable in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(variable, str(tmp_path))
    renderer.ensure_browser_libs()
    require_browser(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 800, "height": 600})

        def sample(content, *, full=False):
            page.set_content('<style>body{margin:0}.slide{position:relative;width:800px;height:600px;'
                             'overflow:hidden;color:#111;background:white;font:24px sans-serif}'
                             'svg{display:block}p{margin:0}</style>'
                             '<section class="slide active" data-slide="01">' + content + '</section>')
            before = page.screenshot()
            result = page.evaluate(renderer._LAYOUT_GEOMETRY_JS)
            assert page.screenshot() == before
            return result if full else result["text_boxes"]

        yield sample
        browser.close()


def test_svg_text_tspan_and_mixed_html_are_complete_without_duplicates(sampled_page):
    rows = sampled_page('<p>Growth <strong>12%</strong> versus <em>last year</em>.</p>'
        '<svg width="600" height="120" aria-hidden="true">'
        '<text x="20" y="50">Revenue <tspan fill="#c60">+18.1%</tspan> total</text>'
        '<text x="20" y="90"><tspan>7</tspan><tspan>月</tspan></text></svg>')
    assert [row["text"] for row in rows] == [
        "Growth", "12%", "versus", "last year", ".", "Revenue", "+18.1%", "total", "7", "月"]
    assert all(row["width"] > 0 and row["height"] > 0 for row in rows)
    assert {"text", "tspan", "strong", "em"} <= {row["tag"] for row in rows}


@pytest.mark.parametrize("hidden", [
    '<div style="display:none">secret display</div>',
    '<div style="visibility:hidden">secret visibility</div>',
    '<div style="opacity:0"><span>secret ancestor opacity</span></div>',
    '<div style="opacity:.1"><span style="opacity:.1">secret cumulative opacity</span></div>',
    '<span style="color:transparent">secret transparent</span>',
    '<span style="-webkit-text-fill-color:transparent">secret fill</span>',
    '<div style="position:absolute;left:900px">secret off canvas</div>',
    '<div style="width:100px;height:20px;overflow:hidden">'
        '<span style="position:relative;top:100px">secret clipped</span></div>',
    '<svg width="200" height="80"><defs><text x="10" y="30">secret definition</text></defs>'
        '<title>secret metadata</title><desc>secret description</desc></svg>',
    '<svg width="200" height="80"><text x="10" y="30" fill="none">secret unpainted</text></svg>',
    '<svg width="200" height="80"><text x="10" y="30" fill-opacity="0">secret fill opacity</text></svg>',
    '<div style="position:absolute;clip:rect(0px,0px,0px,0px)">secret rect</div>',
    '<div style="position:fixed;clip:rect(auto,0px,auto,0px)">secret fixed rect</div>',
    '<div style="position:relative"><h1 style="position:absolute;width:1px;height:1px;'
        'overflow:hidden;clip:rect(0,0,0,0)">secret duplicate heading</h1></div>',
    '<div style="clip-path:inset(50%)"><span>secret inset ancestor</span></div>',
    '<div style="clip-path:inset(0 100% 0 0)">secret horizontal inset</div>',
    '<div style="clip-path:inset(50% round 10px)">secret rounded inset</div>',
    '<div style="transform:rotate(20deg);clip-path:inset(50%)">secret rotated empty clip</div>',
    '<div style="transform:scale(-1);clip-path:inset(50%)">secret reflected empty clip</div>',
    '<div style="position:absolute;width:0;height:0;clip:rect(0,0,0,0)">secret zero box</div>',
])
def test_non_displayed_text_is_not_screen_evidence(sampled_page, hidden):
    actual = sampled_page('<p>visible evidence</p>' + hidden, full=True)
    assert [row["text"] for row in actual["text_boxes"]] == ["visible evidence"]
    assert all("secret" not in row["text"] for row in actual["layout_blocks"])


@pytest.mark.parametrize("position", ["static", "relative"])
def test_legacy_clip_does_not_hide_non_positioned_text(sampled_page, position):
    rows = sampled_page(f'<p style="position:{position};clip:rect(0,0,0,0)">Visible text</p>')
    assert [row["text"] for row in rows] == ["Visible text"]


@pytest.mark.parametrize("clip", ["clip:rect(0px,100px,40px,0px)", "clip-path:inset(0 50% 0 0)"])
@pytest.mark.parametrize("scale", [1, 2])
def test_partial_rectangular_clip_preserves_visible_fragments(sampled_page, clip, scale):
    rows = sampled_page(f'<div style="position:absolute;left:40px;top:40px;width:200px;height:40px;'
        f'font:24px monospace;white-space:nowrap;transform-origin:top left;transform:scale({scale});{clip}">'
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ</div>')
    assert len(rows) == 1
    assert rows[0]["x"] == pytest.approx(40)
    assert rows[0]["width"] > 100 * scale
    assert rows[0]["fragments"]
    assert all(fragment["width"] == pytest.approx(100 * scale) for fragment in rows[0]["fragments"])


def test_partial_canvas_clip_preserves_original_overflow_evidence(sampled_page, tmp_path):
    rows = sampled_page('<p style="position:absolute;left:750px;top:40px;width:max-content">'
                        'Visible beginning but clipped ending</p>')
    assert rows[0]["width"] > 100
    assert rows[0]["fragments"][0]["width"] == pytest.approx(50)
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    facts = core._page_layout_defects(1, {"text_boxes": rows})
    assert any(item["type"] == "text_overflow" and item["overflow_px"]["canvas_edge"] > 50 for item in facts)


def test_hidden_duplicate_title_does_not_create_text_or_container_collision(sampled_page, tmp_path):
    actual = sampled_page('<div style="position:absolute;left:20px;top:40px;width:400px;height:100px">'
        '<h1 style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)">'
        'Duplicated hidden title</h1></div>'
        '<p style="position:absolute;left:20px;top:40px;width:400px;height:100px">Actual title</p>', full=True)
    assert [row["text"] for row in actual["text_boxes"]] == ["Actual title"]
    assert [row["text"] for row in actual["layout_blocks"]] == ["Actual title"]
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    assert not [item for item in core._page_layout_defects(1, actual)
                if item["type"] in {"text_collision", "block_collision"}]


def test_visible_parent_does_not_reintroduce_hidden_child_text(sampled_page):
    actual = sampled_page('<div>Real content<span style="position:absolute;clip-path:inset(50%)">'
                          'Hidden content</span></div>', full=True)
    assert [row["text"] for row in actual["layout_blocks"]] == ["Real content"]


def test_stroke_only_svg_and_visible_child_remain_evidence(sampled_page):
    rows = sampled_page('<p style="visibility:hidden">hidden prefix '
        '<span style="visibility:visible">visible child</span></p>'
        '<svg width="400" height="80"><text x="10" y="50" fill="none" '
        'stroke="black" stroke-width="1">outlined SVG</text></svg>')
    assert [row["text"] for row in rows] == ["visible child", "outlined SVG"]


def test_wrapped_inline_text_has_no_false_collision(sampled_page, tmp_path):
    rows = sampled_page('<p style="width:220px;line-height:1.5">Several words before '
        '<span class="body-highlight">the highlighted phrase across lines</span>'
        ' and several more words after the highlighted phrase.</p>')
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    assert any(core._boxes_overlap(left, right) for i, left in enumerate(rows) for right in rows[i + 1:])
    assert all(row["fragments"] for row in rows)
    defects = core._page_layout_defects(1, {"text_boxes": rows})
    assert not [item for item in defects if item["type"] == "text_collision"]


def test_actual_text_overlap_is_still_reported(sampled_page, tmp_path):
    rows = sampled_page('<p style="position:absolute;left:20px;top:40px">Visible collision</p>'
                        '<p style="position:absolute;left:30px;top:42px">Second text</p>')
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    collisions = [item for item in core._page_layout_defects(1, {"text_boxes": rows}) if item["type"] == "text_collision"]
    assert collisions and all(item['severity'] == 'advisory' for item in collisions)
    assert all(item['fragments'] and item['overlap_ratio'] > 0 for item in collisions)
    assert core._page_render_quality_status(collisions, []) == 'advisory'


def test_css_class_renaming_does_not_change_geometry_judgment(sampled_page, tmp_path):
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    outputs = []
    for name in ('body-copy', 'watermark-decorative', 'unrelated-name'):
        rows = sampled_page(f'<p class="{name}" style="position:absolute;left:20px;top:40px">Same text</p>'
                            '<p style="position:absolute;left:30px;top:42px">Overlapping text</p>')
        observations = core._page_layout_defects(1, {'text_boxes': rows})
        outputs.append([{key: item[key] for key in ('id', 'type', 'severity', 'overlap_ratio', 'fragments')}
                        for item in observations if item['type'] == 'text_collision'])
    assert outputs[0] and outputs[0] == outputs[1] == outputs[2]


def test_top_n_without_component_class_is_not_a_missing_item_alarm(sampled_page, tmp_path):
    rows = sampled_page('<h1>Top 3</h1><ol><li>One</li><li>Two</li><li>Three</li></ol>')
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    observations = core._page_layout_defects(1, {'text_boxes': rows})
    assert not any(item['type'] in {'top_n_count_mismatch', 'missing_items'} for item in observations)


def test_portrait_source_in_landscape_cover_reports_actual_crop_ratio(tmp_path):
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    observations = core._undersized_content_bitmaps([
        {'width': 600, 'height': 100, 'natural_width': 100, 'natural_height': 600, 'object_fit': 'cover'}])
    assert observations[0]['crop_mismatch'] == 36.0


def test_deck291_wrapped_chinese_paragraph_with_bold_is_not_a_collision(sampled_page, tmp_path):
    rows = sampled_page('<p style="width:260px;line-height:1.5">数量方向<b>取决于两次移动的相对幅度</b>'
                        '：供给减少压低它，需求增加抬高它。只凭移动方向无法确定最终数量。</p>')
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    assert any(core._boxes_overlap(left, right) for i, left in enumerate(rows) for right in rows[i + 1:])
    assert not [item for item in core._page_layout_defects(1, {"text_boxes": rows}) if item["type"] == "text_collision"]


def test_prose_and_source_literals_are_not_semantic_or_typography_verdicts(sampled_page, tmp_path):
    rows = sampled_page('<p>Explain TODO, TBD and 待补; R17 and R-S1 are quoted examples.</p>')
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    (tmp_path / 'knowledge.md').write_text('derived: 37.2; this is not the page output contract.')
    outputs = []
    for source in ('<script>const unused={fontSize:1}; // echarts.init()\n</script>',
                   '<script>const unused={fontSize:100};</script>'):
        (tmp_path / 'slides/slide_01.html').write_text(source)
        actual = {'text_boxes': rows, 'media': [{'x': 0, 'y': 0, 'width': 800, 'height': 600}],
                  'text': 'TODO TBD 待补 R17 R-S1', 'frame': 'freeform'}
        outputs.append(core._geometry_audit(tmp_path, [{'number': 1}],
                                            {'pages': [{'page': 1, 'geometry': actual}]}))
    assert outputs[0] == outputs[1]
    assert outputs[0]['status'] == 'PASS' and outputs[0]['warnings'] == []


def test_audit_does_not_impose_class_named_special_frames_or_dark_mask_style(sampled_page, tmp_path):
    from PIL import Image
    rows = sampled_page('<h1 style="margin:0">Cover 01</h1>')
    core = workspace_skill_module(tmp_path, "deck_core")
    core._read_author_geometry(tmp_path)
    Image.new('RGB', (800, 600), '#080808').save(tmp_path / 'renders/slide_01.png')
    outputs = []
    for name in ('title', 'watermark-decorative', 'ordinary'):
        actual = {'text_boxes': [{**row, 'class_name': name} for row in rows],
                  'frame': name, 'special_layout': name, 'boxes': {},
                  'media': [{'x': 0, 'y': 0, 'width': 800, 'height': 600}]}
        outputs.append(core._geometry_audit(tmp_path, [{'number': 1}],
                                            {'pages': [{'page': 1, 'geometry': actual}]}))
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0]['status'] == 'PASS' and outputs[0]['warnings'] == []


def test_image_geometry_does_not_infer_evidence_or_decoration_from_names(tmp_path):
    write_geometry(tmp_path, geometry(width_px=800, height_px=600))
    core = workspace_skill_module(tmp_path, 'deck_core')
    core._read_author_geometry(tmp_path)
    observations = []
    for name in ('evidence-chart', 'decorative-icon', 'background'):
        measured = core._undersized_content_bitmaps([{'src': name + '.png', 'alt': name, 'class_name': name,
            'width': 70, 'height': 40, 'natural_width': 140, 'natural_height': 80, 'object_fit': 'cover'}])
        observations.append([{key: value for key, value in row.items() if key not in {'src', 'alt'}} for row in measured])
    assert observations[0] and observations[0] == observations[1] == observations[2]


def test_block_sampling_and_parent_identity_do_not_depend_on_css_class_names(sampled_page, tmp_path):
    core = workspace_skill_module(tmp_path, 'deck_core')
    core._read_author_geometry(tmp_path)
    outputs = []
    for name in ('content-stage', 'composition__primary', 'arbitrary-wrapper'):
        actual = sampled_page(f'<div class="{name}" style="position:relative;width:500px;height:200px">'
            '<div style="position:absolute;left:0;top:20px;width:200px;height:70px">First block</div>'
            '<div style="position:absolute;left:20px;top:25px;width:200px;height:70px">Second block</div></div>'
            '<div class="same-name" style="position:relative"><div>Elsewhere</div></div>', full=True)
        blocks = actual['layout_blocks']
        assert len(blocks) >= 5
        assert blocks[1]['parent_key'] == blocks[2]['parent_key']
        assert blocks[1]['parent_key'] != blocks[-1]['parent_key']
        observations = core._page_layout_defects(1, actual)
        outputs.append([{key: row[key] for key in ('id', 'type', 'severity', 'overlap_ratio', 'fragments')}
                        for row in observations if row['type'] == 'block_collision'])
    assert outputs[0] and outputs[0] == outputs[1] == outputs[2]
