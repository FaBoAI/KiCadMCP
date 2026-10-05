"""Resolve tool argument paths inside an explicit MCP workspace."""
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory")

    def path(self, value: str) -> str:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Path must stay inside the configured workspace")
        return str(candidate)
