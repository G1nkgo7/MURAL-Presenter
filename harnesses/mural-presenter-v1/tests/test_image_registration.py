"""显式图片声明进入唯一 ArtifactStore；Markdown 不参与反向推断。"""
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from PIL import Image
import pytest

from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState


@pytest.fixture
def core(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"))
    from _internal import deck_core
    return deck_core


def _root(tmp_path):
    root = WorkspaceState(tmp_path / "deck").paths.root
    (root / "base.css").write_text(":root {--canvas-w:1600px;--canvas-h:900px}")
    return root


def _receipts(text):
    return [json.loads(line.removeprefix("MURAL_ASSET ")) for line in text.splitlines()
            if line.startswith("MURAL_ASSET ")]


def _commit(root, receipt):
    return ArtifactStore(root).record_image_result(owner_id="image-owner", **receipt)


@pytest.mark.parametrize("content", ["broken json", '{"assets": []}', '{"assets": {"old": 2}}'])
def test_corrupt_download_receipt_is_not_overwritten_as_empty(core, tmp_path, content):
    root = _root(tmp_path)
    path = core._download_receipt_path(root)
    path.write_text(content)
    with pytest.raises(ValueError, match="image_download_receipt_invalid"):
        core._record_download_receipts(root, [])
    assert path.read_text() == content


def test_receipt_io_failure_is_distinct_from_not_created(core, tmp_path, monkeypatch):
    root = _root(tmp_path)
    assert core._load_download_receipts(root) == {}
    original = Path.read_text
    def unreadable(path, *args, **kwargs):
        if path == core._download_receipt_path(root):
            raise PermissionError("fixture denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ValueError, match="image_download_receipt_io.*fixture denied"):
        core._load_download_receipts(root)


@pytest.mark.parametrize("field", ["path", "sha256", "download", "missing"])
def test_download_receipt_failure_reports_exact_difference(core, field):
    entry = {"id": "hero", "path": "assets/hero.png", "source": "article", "download": "https://example.test/hero.png"}
    receipt = {**entry, "sha256": "current-hash"}
    if field == "missing":
        receipt = {}
    else:
        receipt[field] = "previous"
    with pytest.raises(ValueError, match="image_download_receipt_" + ("missing" if field == "missing" else "mismatch")) as error:
        core._validate_download_receipt(entry, receipt, "current-hash", required=True)
    assert field in str(error.value)
    if field in {"path", "sha256"}:
        assert "fetch --replace" not in str(error.value)


def test_source_metadata_change_does_not_redownload_or_rewrite_download_fact(core, tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    image = root / "assets/hero.png"
    Image.new("RGB", (30, 20), "navy").save(image)
    entry = {"id": "hero", "path": "assets/hero.png", "source": "https://example.test/old-article",
             "download": "https://example.test/original.png", "kind": "real", "status": "pending"}
    core._record_download_receipts(root, [entry])
    before = core._download_receipt_path(root).read_bytes()
    changed = {**entry, "source": "https://example.test/corrected-article"}
    monkeypatch.setattr(core, "_registered_images", lambda root: [changed])
    monkeypatch.setattr(core, "_download_image_payload", lambda *a, **kw: pytest.fail("metadata correction is not a new download"))
    core.fetch_images(root)
    assert core._download_receipt_path(root).read_bytes() == before
    output = capsys.readouterr().out
    assert "source_metadata_changed" in output and "reused:1" in output


def test_plain_attachment_crop_full_extent_preserves_alpha_and_actual_encoding(core, tmp_path, capsys):
    root = _root(tmp_path)
    Image.new("RGBA", (1200, 900), (10, 30, 60, 128)).save(root / "inputs/chart.png")
    core.material_figure(root, "inputs/chart.png", "assets/figure.png", "0,0,1,1")
    result = json.loads(capsys.readouterr().out)
    assert result["pixels"] == [1200, 900]
    assert result["has_alpha"] is True
    assert result["layout_evidence"]["text"] == "unavailable"
    with Image.open(root / "assets/figure.png") as image:
        assert image.format == "PNG" and image.size == (1200, 900)
        assert image.getpixel((600, 450)) == (10, 30, 60, 128)
    core.register_image(root, "figure", "assets/figure.png", kind="material", slides=[1])
    receipt = _receipts(capsys.readouterr().out)[0]
    record = _commit(root, receipt)
    assert record.pixel_width == 1200 and record.pixel_height == 900


def test_crop_and_user_image_use_actual_upright_exif_dimensions(core, tmp_path, capsys):
    root = _root(tmp_path)
    image = Image.new("RGB", (900, 600), "navy")
    exif = Image.Exif()
    exif[274] = 6
    image.save(root / "inputs/photo.jpg", exif=exif)
    core.material_figure(root, "inputs/photo.jpg", "assets/cropped.png", "0,0,1,1")
    assert json.loads(capsys.readouterr().out)["pixels"] == [600, 900]
    core.register_user_image(root, "inputs/photo.jpg", "assets/user.png")
    assert json.loads(capsys.readouterr().out)["pixels"] == [600, 900]
    for filename in ("cropped.png", "user.png"):
        with Image.open(root / "assets" / filename) as actual:
            assert actual.format == "PNG" and actual.size == (600, 900)


@pytest.mark.parametrize("size", [(1, 1), (16, 16), (120, 20)])
def test_user_image_dimensions_are_evidence_not_a_usability_gate(core, tmp_path, capsys, size):
    root = _root(tmp_path)
    source = root / "inputs/icon.png"
    Image.new("RGBA", size, (10, 20, 30, 128)).save(source)
    core.register_user_image(root, "inputs/icon.png", "assets/icon.png")
    result = json.loads(capsys.readouterr().out)
    assert result["pixels"] == list(size)
    with Image.open(root / "assets/icon.png") as output:
        assert output.size == size and output.getpixel((0, 0)) == (10, 20, 30, 128)
    registry = json.loads((root / "_trace/user-images.json").read_text())
    record = registry["assets"]["assets/icon.png"]
    assert record["source"] == "inputs/icon.png"
    assert record["sha256"] == hashlib.sha256((root / "assets/icon.png").read_bytes()).hexdigest()


def test_user_image_still_requires_valid_pixels_and_in_scope_paths(core, tmp_path):
    root = _root(tmp_path)
    (root / "inputs/invalid.png").write_bytes(b"not an image")
    with pytest.raises(Image.UnidentifiedImageError):
        core.register_user_image(root, "inputs/invalid.png", "assets/invalid.png")
    outside = tmp_path / "outside.png"
    Image.new("RGBA", (16, 16)).save(outside)
    (root / "inputs/escape.png").symlink_to(outside)
    with pytest.raises(ValueError, match="under inputs"):
        core.register_user_image(root, "inputs/escape.png", "assets/escape.png")
    assert not (root / "assets/invalid.png").exists()
    assert not (root / "assets/escape.png").exists()


@pytest.mark.parametrize("kind", ["material", "user"])
def test_parallel_derivatives_preserve_all_provenance(core, tmp_path, monkeypatch, kind):
    root = _root(tmp_path)
    Image.new("RGB", (1200, 900), "orange").save(root / "inputs/source.png")
    registry_path = root / "_trace" / ("material-figures.json" if kind == "material" else "user-images.json")
    registry_path.write_text(json.dumps({"schema": "mural." + registry_path.stem + ".v1", "assets": {"existing": {"keep": True}}}))
    barrier = threading.Barrier(4)
    write_bytes, read_text = core.atomic_write_bytes, Path.read_text
    def synchronize(root, path, payload):
        write_bytes(root, path, payload)
        barrier.wait(timeout=10)
    def slow_read(path, *args, **kwargs):
        value = read_text(path, *args, **kwargs)
        if path == registry_path:
            time.sleep(0.04)
        return value
    monkeypatch.setattr(core, "atomic_write_bytes", synchronize)
    monkeypatch.setattr(Path, "read_text", slow_read)
    def create(index):
        output = f"assets/derived-{index}.png"
        if kind == "material":
            core.material_figure(root, "inputs/source.png", output, "0,0,1,1")
        else:
            core.register_user_image(root, "inputs/source.png", output)
        return output
    with ThreadPoolExecutor(4) as executor:
        outputs = list(executor.map(create, range(4)))
    registry = json.loads(registry_path.read_text())
    assert set(registry["assets"]) == {"existing", *outputs}
    assert registry["assets"]["existing"] == {"keep": True}
    for index, output in enumerate(outputs):
        assert registry["assets"][output]["source"] == "inputs/source.png"
        core.register_image(root, f"derived-{index}", output, kind=kind, slides=[1])


@pytest.mark.parametrize("kind", ["material", "user"])
def test_corrupt_provenance_is_not_silently_replaced(core, tmp_path, kind):
    root = _root(tmp_path)
    Image.new("RGB", (1200, 900), "orange").save(root / "inputs/source.png")
    registry_path = root / "_trace" / ("material-figures.json" if kind == "material" else "user-images.json")
    registry_path.write_text('{"assets": truncated')
    before = registry_path.read_bytes()
    with pytest.raises(json.JSONDecodeError):
        if kind == "material":
            core.material_figure(root, "inputs/source.png", "assets/derived.png", "0,0,1,1")
        else:
            core.register_user_image(root, "inputs/source.png", "assets/derived.png")
    assert registry_path.read_bytes() == before


@pytest.mark.parametrize("box", ["10,20,900,800", "nan,0,1,1", "0,0,inf,1", "0.8,0,0.2,1", "0,0,1"])
def test_crop_never_guesses_pixel_coordinates(core, tmp_path, box):
    root = _root(tmp_path)
    Image.new("RGB", (1200, 900)).save(root / "inputs/chart.png")
    with pytest.raises(ValueError, match="--box"):
        core.material_figure(root, "inputs/chart.png", "assets/bad.png", box)
    assert not (root / "assets/bad.png").exists()


def test_crop_rejects_symlink_escape_but_allows_document_full_extent(core, tmp_path):
    root = _root(tmp_path)
    source = tmp_path / "outside.png"
    Image.new("RGB", (1200, 900)).save(source)
    (root / "inputs/escape.png").symlink_to(source)
    with pytest.raises(ValueError, match="under inputs"):
        core.material_figure(root, "inputs/escape.png", "assets/bad.png", "0,0,1,1")
    page = root / "inputs/extracted/attachment/pages/page_001.png"
    page.parent.mkdir(parents=True)
    Image.new("RGB", (1200, 900)).save(page)
    core.material_figure(root, str(page.relative_to(root)), "assets/full-page.png", "0,0,1,1")
    assert (root / "assets/full-page.png").is_file()
    (root / "assets/escape.png").symlink_to(source)
    with pytest.raises(ValueError, match="directly under assets"):
        core.material_figure(root, str(page.relative_to(root)), "assets/escape.png", "0.1,0.1,0.9,0.7")
    assert not (root / "assets/bad.png").exists()


@pytest.mark.parametrize("box,pixels,area", [
    ("0.04,0.04,0.96,0.96", [920, 736], 0.8464),
    ("0,0,0.5,1", [500, 800], 0.5),
    ("0,0,1,1", [1000, 800], 1.0),
])
@pytest.mark.parametrize("policy", ["preserve-complete-figure", "subject-only", "document-facsimile"])
def test_document_crop_geometry_does_not_classify_purpose(core, tmp_path, capsys, box, pixels, area, policy):
    root = _root(tmp_path)
    source = root / "inputs/extracted/material/pages/page_001.png"
    source.parent.mkdir(parents=True)
    Image.new("RGBA", (1000, 800), (20, 70, 150, 128)).save(source)
    source.with_suffix(".json").write_text(json.dumps({
        "source_path": "inputs/originals/material.pdf", "page": 7,
        "page_points": [1000, 800],
        "text_blocks": [{"bbox_pdf": [0, 0, 1000, 800], "text": "Figure labels", "chars": 700}],
    }))
    original = source.read_bytes()
    core.material_figure(root, source.relative_to(root).as_posix(), "assets/figure.png", box,
                         crop_policy=policy)
    result = json.loads(capsys.readouterr().out)
    entry = json.loads((root / "_trace/material-figures.json").read_text())["assets"]["assets/figure.png"]
    assert result["status"] == "PASS" and result["pixels"] == pixels
    assert result["page_area"] == entry["page_area"] == pytest.approx(area)
    assert result["crop_policy"] == entry["crop_policy"] == policy
    assert result["quality_status"] == entry["quality_status"] == "unreviewed"
    assert result["requires_full_resolution_review"] and entry["requires_full_resolution_review"]
    assert result["edge_text_fragments"] == entry["edge_text_fragments"]
    assert entry["source_document"] == "inputs/originals/material.pdf" and entry["source_page"] == 7
    assert entry["native_text_chars"] == result["native_text_chars"] == 700
    assert entry["native_text_coverage"] == result["native_text_coverage"]
    assert "facsimile" not in entry and "facsimile_justification" not in entry
    assert not entry.get("warnings")
    with Image.open(root / "assets/figure.png") as image:
        assert list(image.size) == pixels and image.getpixel((0, 0)) == (20, 70, 150, 128)
    assert source.read_bytes() == original
    assert entry["sha256"] == hashlib.sha256((root / "assets/figure.png").read_bytes()).hexdigest()


@pytest.mark.parametrize("size", [(399, 320), (800, 179), (400, 180), (656, 331), (1, 1)])
def test_small_crop_can_be_registered_without_automatic_usability_verdict(core, tmp_path, capsys, size):
    root = _root(tmp_path)
    source = root / "inputs/figure.png"
    Image.new("RGBA", size, (10, 30, 60, 128)).save(source)
    original = source.read_bytes()
    core.material_figure(root, "inputs/figure.png", "assets/figure.png", "0,0,1,1")
    result = json.loads(capsys.readouterr().out)
    assert result["pixels"] == list(size) and result["quality_status"] == "unreviewed"
    entry = json.loads((root / "_trace/material-figures.json").read_text())["assets"]["assets/figure.png"]
    assert not entry.get("warnings")
    core.register_image(root, "figure", "assets/figure.png", kind="material", slides=[1])
    record = _commit(root, _receipts(capsys.readouterr().out)[0])
    assert (record.pixel_width, record.pixel_height) == size
    with Image.open(root / "assets/figure.png") as image:
        assert image.size == size and image.getpixel((0, 0)) == (10, 30, 60, 128)
    assert source.read_bytes() == original


@pytest.mark.parametrize("box", ["0,0,0.01,1", "0,0,1,0.01"])
def test_crop_rejects_empty_rounded_pixel_region_without_overwriting(core, tmp_path, capsys, box):
    root = _root(tmp_path)
    Image.new("RGB", (20, 20), "navy").save(root / "inputs/source.png")
    core.material_figure(root, "inputs/source.png", "assets/crop.png", "0,0,1,1")
    capsys.readouterr()
    image_before = (root / "assets/crop.png").read_bytes()
    receipt_before = (root / "_trace/material-figures.json").read_bytes()
    with pytest.raises(ValueError, match="empty pixel region"):
        core.material_figure(root, "inputs/source.png", "assets/crop.png", box)
    assert (root / "assets/crop.png").read_bytes() == image_before
    assert (root / "_trace/material-figures.json").read_bytes() == receipt_before


@pytest.mark.parametrize("contents", [b"", b"not an image"])
def test_crop_rejects_undecodable_source_without_changing_existing_output(core, tmp_path, capsys, contents):
    root = _root(tmp_path)
    Image.new("RGB", (40, 40), "navy").save(root / "inputs/source.png")
    core.material_figure(root, "inputs/source.png", "assets/crop.png", "0,0,1,1")
    capsys.readouterr()
    image_before = (root / "assets/crop.png").read_bytes()
    receipt_before = (root / "_trace/material-figures.json").read_bytes()
    (root / "inputs/bad.png").write_bytes(contents)
    with pytest.raises((ValueError, OSError)):
        core.material_figure(root, "inputs/bad.png", "assets/crop.png", "0,0,1,1")
    assert (root / "assets/crop.png").read_bytes() == image_before
    assert (root / "_trace/material-figures.json").read_bytes() == receipt_before


@pytest.mark.parametrize("command", ["crop", "crop-material"])
def test_crop_cli_allows_small_full_document_without_justification(tmp_path, command):
    root = _root(tmp_path)
    source = root / "inputs/extracted/material/pages/page_001.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (160, 90), (10, 30, 60)).save(source)
    script = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/image.py"
    result = subprocess.run([sys.executable, str(script), command, str(root),
        "--source", source.relative_to(root).as_posix(), "--output", "assets/crop.png",
        "--box", "0,0,1,1", "--crop-policy", "document-facsimile", "--preserve-internal-labels",
        "--auto-tighten"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["pixels"] == [160, 90] and receipt["quality_status"] == "unreviewed"
    assert receipt["crop_policy"] == "document-facsimile" and not receipt["auto_tightened"]
    entry = json.loads((root / "_trace/material-figures.json").read_text())["assets"]["assets/crop.png"]
    assert entry["preserve_internal_labels"] is True
    assert entry["sha256"] == hashlib.sha256((root / "assets/crop.png").read_bytes()).hexdigest()


def test_registration_and_finalize_preserve_guidance_without_reading_catalog(core, tmp_path, capsys):
    root = _root(tmp_path)
    Image.new("RGB", (1200, 900), "navy").save(root / "assets/hero.png")
    crop = "Keep the book and glasses.\nAlign the left edge.\n\nDo not crop the lower shelf."
    metadata = dict(kind="generated", slides=[2, 1], crop=crop, subject_region="lower third\nbook + glasses",
                    text_safe_region="upper left\navoid the window", notes="no decorative frame\n  preserve this indent")
    core.register_image(root, "hero", "assets/hero.png", **metadata)
    receipt = _receipts(capsys.readouterr().out)[0]
    assert receipt["image_metadata"]["crop"] == crop
    assert not (root / "_state/artifacts.json").exists()
    _commit(root, receipt)
    state_path = root / "_state/artifacts.json"
    before = state_path.read_bytes()
    catalog = root / "assets/catalog.md"
    catalog.write_text("## Wrong Markdown\n- path: assets/not-real.png\n- crop: |\n  LOST\n")
    core.finalize_assets(root)
    first = catalog.read_text()
    assert "Wrong Markdown" not in first and "not-real" not in first
    for key in ("crop", "subject_region", "text_safe_region", "notes"):
        assert f"- {key}: " + metadata[key].replace("\n", "\n  ") in first
    assert "bitmap_ready:01,02" in capsys.readouterr().out
    core.finalize_assets(root)
    assert catalog.read_text() == first and state_path.read_bytes() == before
    assert not hasattr(core, "_parse_catalog")


def test_pending_registration_is_not_a_ready_asset_even_if_file_exists(core, tmp_path, capsys):
    root = _root(tmp_path)
    Image.new("RGB", (1200, 900)).save(root / "assets/future.png")
    core.register_image(root, "future", "assets/future.png", status="pending", kind="real", slides=[1],
                        download="https://example.test/future.png")
    _commit(root, _receipts(capsys.readouterr().out)[0])
    core.finalize_assets(root)
    assert "bitmap_ready:none" in capsys.readouterr().out
    assert "- status: pending" in (root / "assets/catalog.md").read_text()


def test_catalog_exposes_image_geometry_without_automatic_full_bleed_verdict(core, tmp_path, capsys):
    root = _root(tmp_path)
    Image.new('RGB', (1600, 900), 'black').save(root / 'assets/solid.png')
    core.register_image(root, 'solid', 'assets/solid.png', kind='generated', slides=[1], display='full-bleed')
    _commit(root, _receipts(capsys.readouterr().out)[0])
    core.finalize_assets(root)
    output = capsys.readouterr().out
    catalog = (root / 'assets/catalog.md').read_text()
    assert 'pixel_size=1600x900' in output and 'full_bleed_scale=1.0' in output
    assert 'full_bleed_ready' not in catalog and 'full-bleed-ready' not in output
    assert '- display: full-bleed' in catalog
    assert (root / 'assets/solid.png').is_file()


def test_catalog_marks_original_retained_images_as_reference_only(core, tmp_path, capsys):
    root = _root(tmp_path)
    Image.new("RGB", (1600, 900), "black").save(root / "assets/original.png")
    core.register_image(
        root,
        "original",
        "assets/original.png",
        kind="real",
        slides=[2],
        purpose="P2照片原图留存；实际版面优先 assets/crop.png",
        display="原件留存，不与预裁图重复上屏",
    )
    _commit(root, _receipts(capsys.readouterr().out)[0])
    core.finalize_assets(root)
    catalog = (root / "assets/catalog.md").read_text()
    assert "- usage_role: reference_only" in catalog


def test_empty_finalize_is_an_empty_projection_not_ready_or_directory_registration(core, tmp_path, capsys):
    root = _root(tmp_path)
    Image.new("RGB", (20, 20)).save(root / "assets/unused-experiment.png")
    store = ArtifactStore(root)
    state = store.path.read_bytes()
    core.finalize_assets(root)
    output = capsys.readouterr().out
    assert "status:PASS" in output and "catalog_state:empty" in output
    assert "assets:0" in output and "bitmap_ready:none" in output
    assert store.path.read_bytes() == state and store.catalog()["images"] == []
    catalog = (root / "assets/catalog.md").read_text()
    assert "catalog_state: empty" in catalog and "unused-experiment" not in catalog
    assert not (root / "assets/contact-sheet.png").exists()


def test_scoped_finalize_does_not_validate_other_authors_inflight_files(core, tmp_path, capsys):
    root = _root(tmp_path)
    store = ArtifactStore(root)
    for name, color in (("cover", "navy"), ("figure", "orange")):
        Image.new("RGB", (800, 450), color).save(root / f"assets/{name}.png")
        core.register_image(root, name, f"assets/{name}.png", kind="generated", slides=[1])
        store.record_image_result(owner_id=name, **_receipts(capsys.readouterr().out)[0])
    state = store.path.read_bytes()
    figure = root / "assets/figure.png"
    original = figure.read_bytes()
    figure.write_bytes(b"in-flight replacement")
    core.finalize_assets(root, owner_id="cover")
    output = capsys.readouterr().out
    assert "assets:1" in output and "figure: pixel_size" not in output
    assert "## cover" in (root / "assets/catalog.md").read_text()
    assert "## figure" in (root / "assets/catalog.md").read_text()
    sheets = list((root / "assets").glob("contact-sheet-*.png"))
    assert len(sheets) == 1
    cover_sheet = sheets[0].read_bytes()
    with pytest.raises(ValueError, match="changed since commit"):
        core.finalize_assets(root, owner_id="figure")
    figure.write_bytes(original)
    core.finalize_assets(root, owner_id="figure")
    assert len(list((root / "assets").glob("contact-sheet-*.png"))) == 2
    assert sheets[0].read_bytes() == cover_sheet and store.path.read_bytes() == state


@pytest.mark.parametrize("problem", ["consumer", "missing", "changed"])
def test_finalize_rejects_drift_without_rewriting_registration(core, tmp_path, capsys, monkeypatch, problem):
    root = _root(tmp_path)
    image = root / "assets/hero.png"
    Image.new("RGB", (1200, 900), "navy").save(image)
    core.register_image(root, "hero", "assets/hero.png", kind="generated", slides=[1, 3])
    _commit(root, _receipts(capsys.readouterr().out)[0])
    state = root / "_state/artifacts.json"
    original = state.read_bytes()
    catalog = root / "assets/catalog.md"
    catalog.write_text("existing projection\n")
    if problem == "consumer":
        monkeypatch.setattr(core, "_load_plans", lambda _root: ({}, [{"number": 1}]))
        message = "unknown slide references 03"
    elif problem == "missing":
        image.unlink()
        message = "missing or empty"
    else:
        Image.new("RGB", (1200, 900), "blue").save(image)
        message = "changed since commit"
    with pytest.raises(ValueError, match=message):
        core.finalize_assets(root)
    assert state.read_bytes() == original and catalog.read_text() == "existing projection\n"


def test_fetch_partial_results_commit_success_and_reuse_only_real_receipts(core, tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    for identity in ("good", "broken"):
        core.register_image(root, identity, f"assets/{identity}.png", status="pending", kind="real", slides=[1],
                            source="https://example.test/article", download=f"https://example.test/{identity}.png")
        _commit(root, _receipts(capsys.readouterr().out)[0])
    image = BytesIO()
    Image.new("RGB", (900, 600), "blue").save(image, format="JPEG")
    calls = []
    def download(url, **_kwargs):
        calls.append(url)
        return image.getvalue() if "good" in url else b"invalid image"
    monkeypatch.setattr(core, "_download_image_payload", download)
    with pytest.raises(ValueError, match="per-asset results"):
        core.fetch_images(root)
    receipts = _receipts(capsys.readouterr().out)
    assert {row["artifact_id"]: row["status"] for row in receipts} == {"good": "available", "broken": "unavailable"}
    for receipt in receipts:
        _commit(root, receipt)
    store = ArtifactStore(root)
    assert store.artifact("good").status.value == "committed"
    assert store.artifact("broken").status.value == "unavailable"
    with Image.open(root / "assets/good.png") as actual:
        assert actual.format == "PNG" and actual.size == (900, 600)
    core.fetch_images(root)
    assert len(calls) == 2
    entry = next(row for row in core._registered_images(root) if row["id"] == "good")
    assert core._download_image_asset(root, entry, False) == ("good", "reused")
    entry["download"] = "https://example.test/different.png"
    with pytest.raises(ValueError, match="image_download_receipt_mismatch.*download"):
        core._download_image_asset(root, entry, False)


def test_register_rejects_wrong_encoding_and_unverified_material(core, tmp_path):
    root = _root(tmp_path)
    Image.new("RGB", (900, 600)).save(root / "assets/false.png", format="JPEG")
    with pytest.raises(ValueError, match="bytes are JPEG"):
        core.register_image(root, "false", "assets/false.png", kind="generated")
    Image.new("RGB", (900, 600)).save(root / "assets/unverified.png")
    with pytest.raises(ValueError, match="current image.py"):
        core.register_image(root, "unverified", "assets/unverified.png", kind="material")


def test_image_cli_explicit_flags_and_normalized_help(tmp_path):
    root = _root(tmp_path)
    script = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/image.py"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    help_result = subprocess.run([sys.executable, str(script), "crop-material", "--help"], capture_output=True, text=True, env=env)
    assert help_result.returncode == 0
    assert "Normalized x0,y0,x1,y1" in help_result.stdout and "never inferred" in help_result.stdout
    result = subprocess.run([sys.executable, str(script), "register", str(root), "--id", "future",
        "--path", "assets/future.png", "--kind", "real", "--status", "pending", "--slides", "1,3",
        "--download", "https://example.test/image.png", "--crop", "first line\nsecond line",
        "--subject-region", "book", "--text-safe-region", "upper left"], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    receipt = _receipts(result.stdout)[0]
    assert receipt["image_metadata"]["crop"] == "first line\nsecond line"
    assert receipt["image_metadata"]["slides"] == [1, 3]
    Image.new("RGB", (1200, 900)).save(root / "inputs/crop.png")
    cropped = subprocess.run([sys.executable, str(script), "crop", str(root), "--source", "inputs/crop.png",
        "--output", "assets/cropped.png", "--box", "0,0,1,1"], capture_output=True, text=True, env=env)
    assert cropped.returncode == 0, cropped.stdout + cropped.stderr
    assert json.loads(cropped.stdout)["pixels"] == [1200, 900]
    (root / "inputs/invalid.png").write_bytes(b"not an image")
    invalid = subprocess.run([sys.executable, str(script), "crop-material", str(root),
        "--source", "inputs/invalid.png", "--output", "assets/invalid.png", "--box", "0,0,1,1"],
        capture_output=True, text=True, env=env)
    assert invalid.returncode == 2 and "status:FAIL" in invalid.stdout
    assert "Traceback" not in invalid.stderr and not (root / "assets/invalid.png").exists()


@pytest.mark.parametrize("root_position", [2, 6])
def test_image_cli_executes_workspace_after_options(tmp_path, root_position):
    root = _root(tmp_path)
    Image.new("RGB", (40, 30)).save(root / "assets/hero.png")
    script = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/image.py"
    flags = ["--id", "hero", "--path", "assets/hero.png", "--kind", "generated"]
    flags.insert(root_position, str(root))
    result = subprocess.run([sys.executable, str(script), "register", *flags],
                            cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _receipts(result.stdout)[0]["path"] == "assets/hero.png"
    assert not (root / "_state/artifacts.json").exists()


def test_register_user_cli_consumes_actual_attachment_not_manifest(tmp_path):
    root = _root(tmp_path)
    source = root / "inputs/small icon.png"
    Image.new("RGBA", (16, 16), (10, 20, 30, 128)).save(source)
    (root / "inputs/manifest.json").write_text(json.dumps({"path": "inputs/small icon.png"}))
    script = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/image.py"
    result = subprocess.run([sys.executable, str(script), "register-user", str(root),
        "--source", "inputs/small icon.png", "--output", "assets/imported.png"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["pixels"] == [16, 16]
    imported = (root / "assets/imported.png").read_bytes()
    invalid = subprocess.run([sys.executable, str(script), "register-user", str(root),
        "--source", "inputs/manifest.json", "--output", "assets/imported.png"],
        capture_output=True, text=True, check=False)
    assert invalid.returncode == 2 and "status:FAIL" in invalid.stdout
    assert "Traceback" not in invalid.stderr
    assert (root / "assets/imported.png").read_bytes() == imported


def test_knowledge_prose_is_not_reverse_parsed_as_a_content_gate(core, tmp_path):
    root = _root(tmp_path)
    (root / "knowledge.md").write_text('R1 is a component name, not a citation.\nTop 3 means three examples.')
    core.CANVAS_W, core.CANVAS_H = 1600, 900
    observed = {"text_boxes": [{"text": "Top 3: R1, R2, R3 — calculated 12%", "x": 20,
                               "y": 20, "width": 800, "height": 40}], "layout_blocks": []}
    assert core._page_layout_defects(1, observed) == []
