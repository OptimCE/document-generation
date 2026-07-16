"""Path confinement for manifest-controlled template files.

A manifest ships inside a template and is therefore semi-trusted: a malicious or
buggy ``entrypoint`` (``../../etc/passwd``, an absolute path) must not let a
renderer open a file outside the downloaded template directory. This is the
renderer-side guard complementing the one ``template_store`` applies on download.
"""

from __future__ import annotations

from pathlib import Path

from domain.errors import TemplateNotFoundError


def resolve_template_file(root: Path, name: str) -> Path:
    """Resolve ``name`` under ``root``, rejecting traversal and missing files.

    Raises ``TemplateNotFoundError`` (permanent) if the resolved path escapes
    ``root`` or does not exist — a bad entrypoint is a template-authoring error,
    not a transient render failure.
    """
    root_resolved = root.resolve()
    target = (root_resolved / name).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise TemplateNotFoundError(f"template entrypoint escapes the template root: {name!r}")
    if not target.is_file():
        raise TemplateNotFoundError(f"template entrypoint not found: {name!r}")
    return target
