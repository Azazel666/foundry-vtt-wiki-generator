"""Build with mkdocs and swap the live site atomically."""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .render import Site, write_mkdocs_config

log = logging.getLogger(__name__)

KEEP_BUILDS = 3


def build(site: Site, site_root: Path, stamp: datetime, change_days: list) -> Path:
    site_root.mkdir(parents=True, exist_ok=True)
    target = site_root / f"build-{stamp.strftime('%Y%m%d-%H%M%S-%f')}"
    with tempfile.TemporaryDirectory(prefix="wikigen-docs-") as tmp:
        docs = Path(tmp) / "docs"
        docs.mkdir()
        site.render(docs, change_days)
        config = Path(tmp) / "mkdocs.yml"
        write_mkdocs_config(config, site.mkdocs_config(docs))
        result = subprocess.run([sys.executable, "-m", "mkdocs", "build", "--quiet", "--clean", "-f", str(config),
                                 "-d", str(target)], capture_output=True, text=True,
                                env={**os.environ, "NO_MKDOCS_2_WARNING": "1"})
    if result.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"mkdocs build failed: {result.stderr.strip()[-2000:]}")
    return target


def publish(site_root: Path, build_dir: Path, keep: int = KEEP_BUILDS) -> None:
    """A relative link, so the path also resolves in the nginx container. rename() replaces it in one step."""
    current = site_root / "current"
    link = site_root / f".current-{os.getpid()}"
    link.unlink(missing_ok=True)
    os.symlink(build_dir.name, link)
    os.replace(link, current)
    log.info("site_published build=%s", build_dir.name)
    builds = sorted((path for path in site_root.glob("build-*") if path.is_dir()), key=lambda path: path.name,
                    reverse=True)
    for old in builds[keep:]:
        if old != build_dir:
            shutil.rmtree(old, ignore_errors=True)
