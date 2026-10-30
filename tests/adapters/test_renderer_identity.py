"""The renderer identity a result carries: enough to explain a changed sha256."""

from __future__ import annotations

from importlib import metadata

from adapters import renderer_identity as module


def test_identity_names_python_zlib_and_the_rendering_libraries():
    identity = module.renderer_identity()

    assert identity["python"]
    assert identity["zlib"]
    for name in ("pypdf", "openpyxl", "docxtpl", "python-docx", "lxml", "jinja2"):
        assert identity[name] not in ("", module.ABSENT), name


def test_a_missing_library_reads_absent_rather_than_failing(monkeypatch):
    real = metadata.version

    def version(name: str) -> str:
        if name == "weasyprint":
            raise metadata.PackageNotFoundError(name)
        return str(real(name))

    monkeypatch.setattr(module.metadata, "version", version)
    assert module.renderer_identity()["weasyprint"] == module.ABSENT
