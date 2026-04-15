"""
Per-machine config loader.

Reads ~/.klh/config.yaml and exposes paths as pathlib.Path objects.
Fails loudly with a useful message if the file is missing or a referenced
path doesn't exist when it should.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_PATH = Path.home() / ".klh" / "config.yaml"


class ConfigError(RuntimeError):
    pass


@dataclass
class Paths:
    picture_dir: Path
    card_dir: Path
    products_dir: Path
    normalized_dir: Path
    mockups_dir: Path
    listed_dir: Path
    extra_images_dir: Optional[Path] = None
    golden_dir: Optional[Path] = None
    drive_inbox: Optional[Path] = None


@dataclass
class Config:
    paths: Paths
    env_file: Path
    tokens_file: Path
    _raw: dict = field(default_factory=dict, repr=False)


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve() if p else None


def load(config_path: Optional[Path] = None) -> Config:
    """Load and validate ~/.klh/config.yaml.

    Reads the module-level CONFIG_PATH by default so tests can
    monkey-patch `pcfg.CONFIG_PATH` and have it take effect.
    """
    if config_path is None:
        config_path = CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found at {config_path}\n"
            "Create it — see klh-listing-tool/README.md for the format."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    paths_raw = raw.get("paths", {})
    required = ("picture_dir", "card_dir", "products_dir",
                "normalized_dir", "mockups_dir", "listed_dir")
    missing = [k for k in required if k not in paths_raw]
    if missing:
        raise ConfigError(
            f"Missing required paths in {config_path}: {', '.join(missing)}"
        )

    paths = Paths(
        picture_dir=_expand(paths_raw["picture_dir"]),
        card_dir=_expand(paths_raw["card_dir"]),
        products_dir=_expand(paths_raw["products_dir"]),
        normalized_dir=_expand(paths_raw["normalized_dir"]),
        mockups_dir=_expand(paths_raw["mockups_dir"]),
        listed_dir=_expand(paths_raw["listed_dir"]),
        extra_images_dir=_expand(paths_raw.get("extra_images_dir")) if paths_raw.get("extra_images_dir") else None,
        golden_dir=_expand(paths_raw.get("golden_dir")) if paths_raw.get("golden_dir") else None,
        drive_inbox=_expand(paths_raw.get("drive_inbox")) if paths_raw.get("drive_inbox") else None,
    )

    # Auto-create working directories that are expected to exist.
    for d in (paths.normalized_dir, paths.mockups_dir, paths.listed_dir):
        d.mkdir(parents=True, exist_ok=True)

    return Config(
        paths=paths,
        env_file=_expand(raw.get("env_file", "~/.klh/.env")),
        tokens_file=_expand(raw.get("tokens_file", "~/.klh/tokens.json")),
        _raw=raw,
    )


def main():
    """CLI: print the resolved config for debugging."""
    try:
        cfg = load()
    except ConfigError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)

    print("Config loaded from", CONFIG_PATH)
    print()
    for field_name, value in vars(cfg.paths).items():
        exists = "✓" if value and value.exists() else "✗"
        print(f"  {exists}  {field_name:18s} {value}")
    print()
    print(f"  {'✓' if cfg.env_file.exists() else '✗'}  env_file           {cfg.env_file}")
    print(f"  {'✓' if cfg.tokens_file.exists() else '✗'}  tokens_file        {cfg.tokens_file}")


if __name__ == "__main__":
    main()
