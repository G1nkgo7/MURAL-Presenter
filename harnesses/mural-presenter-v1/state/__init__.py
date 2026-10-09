"""工作区路径、页面生命周期与产物事务合同。"""

from .artifacts import ArtifactRecord, ArtifactStatus, ArtifactStore
from runtime.workspace_paths import (
    WORKSPACE_LAYOUT_VERSION,
    WorkspacePathError,
    WorkspacePaths,
)
from .workspace import PageLifecycle, PageSnapshot, WorkspaceState

__all__ = [
    "ArtifactRecord",
    "ArtifactStatus",
    "ArtifactStore",
    "PageLifecycle",
    "PageSnapshot",
    "WORKSPACE_LAYOUT_VERSION",
    "WorkspacePaths",
    "WorkspacePathError",
    "WorkspaceState",
]
