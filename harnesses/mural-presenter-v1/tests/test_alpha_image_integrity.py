"""真实 PIL/OpenCV 回归：透明预览不露底色，分割不硬删贴边主体。"""
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest

from state.workspace import WorkspaceState


@pytest.fixture
def core(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"))
    from _internal import deck_core
    return deck_core


@pytest.mark.parametrize("alpha", [0, 64, 128, 255])
def test_asset_sheet_composites_alpha_without_changing_source(core, tmp_path, alpha):
    source = tmp_path / "assets/subject.png"
    source.parent.mkdir()
    image = Image.new("RGBA", (376, 200), (230, 60, 30, alpha))
    ImageDraw.Draw(image).rectangle((100, 60, 260, 150), fill=(10, 50, 180, 255))
    image.save(source)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    target = core._image_sheet(tmp_path, [("subject", "assets/subject.png")])
    expected = Image.alpha_composite(Image.new("RGBA", image.size, (242, 242, 238, 255)), image)
    with Image.open(target) as sheet:
        assert sheet.getpixel((20, 20)) == expected.getpixel((8, 8))[:3]
        assert sheet.getpixel((212, 112)) == (10, 50, 180)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


@pytest.mark.parametrize("copy_only", [False, True])
def test_asset_sheet_public_path_is_not_a_trace_symlink(core, tmp_path, monkeypatch, copy_only):
    source = tmp_path / "assets/subject.png"
    source.parent.mkdir()
    if copy_only:
        import errno
        def unavailable(*_):
            raise OSError(errno.EXDEV, "fixture filesystem does not support hard links")
        monkeypatch.setattr(core.os, "link", unavailable)
    for color in ("red", "blue"):
        Image.new("RGB", (60, 40), color).save(source)
        target = core._image_sheet(tmp_path, [("subject", "assets/subject.png")])
        assert target.parent == source.parent and not target.is_symlink()
        assert target.resolve().parent == source.parent.resolve()
        assert target.read_bytes() == (tmp_path / "_trace/image" / target.name).read_bytes()


def _subject(position, background):
    image = Image.new("RGB", (400, 300), background)
    boxes = {
        "center": [(100, 60, 299, 239)],
        "left": [(0, 60, 199, 239)],
        "right": [(200, 60, 399, 239)],
        "top": [(100, 0, 299, 179)],
        "bottom": [(100, 120, 299, 299)],
        "four_edges": [(170, 0, 229, 299), (0, 120, 399, 179)],
    }
    for box in boxes[position]:
        ImageDraw.Draw(image).rectangle(box, fill=(20, 40, 65))
    return image


@pytest.mark.parametrize("position", ["center", "left", "right", "top", "bottom", "four_edges"])
@pytest.mark.parametrize("background", [(255, 255, 255), (220, 200, 165)])
def test_real_grabcut_keeps_subject_touching_each_edge(core, position, background):
    image = _subject(position, background)
    expected = np.any(np.asarray(image) != np.asarray(background), axis=2)
    cutout, report = core._alpha_cutout_subject(image)
    alpha = np.asarray(cutout.getchannel("A"))
    assert np.array_equal(np.asarray(cutout)[:, :, :3], np.asarray(image))
    assert np.count_nonzero(expected & (alpha < 128)) == 0
    assert alpha[10, 10] == 0 and alpha[290, 390] == 0
    source_edges = np.concatenate((expected[0], expected[-1], expected[:, 0], expected[:, -1]))
    assert report["edge_contact_ratio"] == pytest.approx(float(source_edges.mean()), abs=0.0001)
    assert report["edge_contact_basis"] == "source_background_difference"
    assert (report["edge_contact_ratio"] > 0) is (position != "center")


@pytest.mark.parametrize("alpha", [64, 128, 255])
def test_existing_alpha_and_hidden_rgb_are_preserved(core, alpha):
    image = Image.new("RGBA", (400, 300), (250, 10, 90, 0))
    ImageDraw.Draw(image).rectangle((0, 60, 199, 239), fill=(20, 40, 65, alpha))
    cutout, report = core._alpha_cutout_subject(image)
    assert np.array_equal(np.asarray(cutout), np.asarray(image))
    assert report["alpha_source"] == "preserved"
    assert report["edge_contact_basis"] == "source_alpha"
    assert report["edge_contact_ratio"] > 0


def test_partial_opacity_does_not_mean_background_is_already_removed(core):
    image = _subject("left", (255, 255, 255)).convert("RGBA")
    image.putalpha(96)
    cutout, report = core._alpha_cutout_subject(image)
    assert cutout.getpixel((0, 150)) == (20, 40, 65, 96)
    assert cutout.getpixel((390, 10))[3] == 0
    assert report["edge_contact_basis"] == "source_background_difference"


@pytest.mark.parametrize("kind", ["transparent", "uniform", "uniform_partial_opacity", "no_stable_background"])
def test_unreliable_or_empty_source_is_not_success(core, kind):
    if kind == "transparent":
        image = Image.new("RGBA", (400, 300), (180, 30, 70, 0))
        reason = "no visible foreground"
    elif kind in {"uniform", "uniform_partial_opacity"}:
        image = Image.new("RGB", (400, 300), (180, 190, 210))
        if kind == "uniform_partial_opacity":
            image.putalpha(96)
        reason = "distinguish"
    else:
        pixels = np.random.default_rng(20260909).integers(0, 256, (300, 400, 3), dtype=np.uint8)
        image = Image.fromarray(pixels)
        reason = "reliable background"
    with pytest.raises(ValueError, match=reason):
        core._alpha_cutout_subject(image)


@pytest.mark.parametrize("native_alpha", [False, True])
def test_extract_subject_publishes_correct_alpha_but_remains_unreviewed(core, tmp_path, capsys, native_alpha):
    root = WorkspaceState(tmp_path / "workspace").paths.root
    image = _subject("left", (255, 255, 255))
    if native_alpha:
        rgba = np.array(image.convert("RGBA"))
        rgba[:, :, 3] = np.where(np.any(rgba[:, :, :3] != 255, axis=2), 96, 0)
        image = Image.fromarray(rgba)
    source = root / "inputs/subject.png"
    image.save(source)
    original = source.read_bytes()
    core.material_figure(root, "inputs/subject.png", "assets/subject.png", "0,0,1,1",
                         crop_policy="subject-only", alpha_cutout=True)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["quality_status"] == "unreviewed"
    assert receipt["requires_full_resolution_review"] is True
    assert receipt["cutout"]["edge_contact_ratio"] > 0
    assert receipt["cutout"]["foreground_ratio"] > 0
    with Image.open(root / "assets/subject.png") as output:
        assert output.mode == "RGBA" and output.getpixel((0, 150))[3] > 0
        if native_alpha:
            assert np.array_equal(np.asarray(output), np.asarray(image))
    assert source.read_bytes() == original


def test_failed_segmentation_does_not_replace_existing_asset(core, tmp_path):
    root = WorkspaceState(tmp_path / "workspace").paths.root
    Image.new("RGB", (400, 300), "white").save(root / "inputs/empty.png")
    output = root / "assets/subject.png"
    Image.new("RGBA", (400, 300), (20, 40, 65, 128)).save(output)
    previous = output.read_bytes()
    with pytest.raises(ValueError, match="distinguish"):
        core.material_figure(root, "inputs/empty.png", "assets/subject.png", "0,0,1,1",
                             crop_policy="subject-only", alpha_cutout=True)
    assert output.read_bytes() == previous
