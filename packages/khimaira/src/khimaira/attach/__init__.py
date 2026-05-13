"""Venv-injection observer machinery.

`khimaira attach <project>` drops khimaira_observer + a .pth file into a
target project's venv site-packages. App writes zero code, sets zero env
vars, installs zero packages — `.pth` files are a stable Python core
feature, not a package-manager install.

Production safety: the venv is gitignored, so khimaira files never end up
in the committed source tree. Production builds use a fresh venv from
the project's manifest, which doesn't include khimaira_observer.

The auto-reattach machinery (registry + watcher + daemon-startup re-pass)
lives in `khimaira.monitor.attach_supervisor` to keep this module
import-cheap for the CLI command.
"""

from .inject import (
    DEFAULT_PYTHON_VERSION_GLOB,
    AttachResult,
    attach_project,
    detach_project,
    detect_venv,
    is_attached,
)
from .registry import (
    REGISTRY_FILE,
    list_attached,
    record_attach,
    record_detach,
)

__all__ = [
    "AttachResult",
    "attach_project",
    "detach_project",
    "detect_venv",
    "is_attached",
    "DEFAULT_PYTHON_VERSION_GLOB",
    "REGISTRY_FILE",
    "list_attached",
    "record_attach",
    "record_detach",
]
