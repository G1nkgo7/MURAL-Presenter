"""启动减法不能移除 Provider 能力或不可变对象校验。"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from runtime import shared_fonts, shared_objects


def test_non_anthropic_startup_and_recovery_do_not_import_unused_dependencies():
    code = """
import sys
import httpx
import infer
from runtime.skill_runtime import SkillRuntime
from runtime.config import ProviderSettings
from runtime.session import create_transport, OpenAIChatCompatibleTransport
from runtime.recovery import classify_provider_exception
transport = create_transport(ProviderSettings(api_format='openai_chat_compatible',
    model='fixture', base_url='https://fixture.invalid/v1'))
assert isinstance(transport, OpenAIChatCompatibleTransport)
assert classify_provider_exception(httpx.ReadTimeout('fixture')).retryable
assert not classify_provider_exception(ValueError('fixture')).retryable
unused = {'anthropic', 'requests', 'rapidocr', 'rapidocr_onnxruntime', 'cv2',
          'pymupdf', 'fitz', 'docx', 'pptx', 'openpyxl'}
assert not unused.intersection(sys.modules), unused.intersection(sys.modules)
from runtime.session import AnthropicTransport
anthropic_transport = AnthropicTransport(ProviderSettings(api_format='anthropic',
    model='fixture', base_url='https://fixture.invalid'))
assert 'anthropic' in sys.modules
assert callable(anthropic_transport._client_type)
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_shared_digest_rechecks_bytes_even_when_size_and_mtime_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "object"
    path.write_bytes(b"before")
    original_open = Path.open
    reads = []

    def track_open(self, mode="r", *args, **kwargs):
        if self == path and mode == "rb":
            reads.append(self)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_open)
    assert shared_objects._sha256(path) == hashlib.sha256(b"before").hexdigest()
    assert shared_fonts._sha256(path) == shared_objects._sha256(path)
    assert len(reads) == 3
    stat = path.stat()
    path.write_bytes(b"edited")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert shared_objects._sha256(path) == hashlib.sha256(b"edited").hexdigest()
    assert len(reads) == 4
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"newone")
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(path)
    assert shared_objects._sha256(path) == hashlib.sha256(b"newone").hexdigest()
    assert len(reads) == 5
    path.unlink()
    with pytest.raises(FileNotFoundError):
        shared_objects._sha256(path)


def test_existing_immutable_font_metadata_is_verified_without_write(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    shared_fonts._atomic_json(path, {"sha256": "fixture"}, immutable=True)
    before = path.stat()
    monkeypatch.setattr(shared_fonts.os, "open", lambda *_a, **_k: pytest.fail("rewrote immutable metadata"))
    shared_fonts._atomic_json(path, {"sha256": "fixture"}, immutable=True)
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(shared_fonts.SharedFontError, match="conflicts"):
        shared_fonts._atomic_json(path, {"sha256": "changed"}, immutable=True)
    assert not list(tmp_path.glob("*.pending"))
