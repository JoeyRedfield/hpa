"""配置与隐私档位判定。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VAULT = Path.home() / "Documents" / "notes"

# 索引时整棵跳过的目录（相对 vault 根，点开头的目录一律跳过）
SKIP_DIRS = {
    ".git",
    ".obsidian",
    ".trash",
    ".tmp",
    ".worktrees",
    ".agents",
    ".claude",
    ".claudian",
    "node_modules",
}
SKIP_NAMES = {"Icon", ".DS_Store"}

# P2 凭证类文件名标记：永不进 prompt、永不进日志
CREDENTIAL_MARKERS = ("密码", "凭证", "账号", "token", "secret", "key")

# 单文件索引上限，防止把巨大的导出文件塞进索引
MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class Config:
    vault: Path = DEFAULT_VAULT
    project_root: Path = PROJECT_ROOT
    max_file_bytes: int = MAX_FILE_BYTES

    @property
    def db_path(self) -> Path:
        return self.project_root / ".cache" / "kb.sqlite3"

    @property
    def state_dir(self) -> Path:
        return self.project_root / "state"

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Config":
        path = config_path or (PROJECT_ROOT / "config.toml")
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        vault = data.get("vault", {}).get("path")
        return cls(vault=Path(vault).expanduser() if vault else DEFAULT_VAULT)


def classify_tier(rel_path: str) -> str:
    """按 spec 第 7 节把 vault 文件分三档：P0 可发模型 / P1 身份私密 / P2 凭证。"""
    parts = Path(rel_path).parts
    if not parts or parts[0] != "private":
        return "P0"
    name = parts[-1].lower()
    if any(marker in name for marker in CREDENTIAL_MARKERS):
        return "P2"
    return "P1"
