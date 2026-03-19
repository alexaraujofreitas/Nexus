# ============================================================
# NEXUS TRADER — Configuration Versioner
# ============================================================
# Snapshot and restore configuration versions with diff support.

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from config.constants import CONFIG_PATH


class ConfigVersioner:
    """
    Configuration snapshot versioning system.

    Maintains timestamped snapshots of config.yaml in a dedicated versions directory
    with support for diffs, comparisons, and automated pruning.

    Attributes:
        config_path (Path): Path to the active config.yaml
        versions_dir (Path): Directory to store version files (config_versions/)
    """

    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        versions_dir: Optional[Path] = None,
    ):
        """
        Initialize the config versioner.

        Args:
            config_path: Path to config.yaml (defaults to CONFIG_PATH)
            versions_dir: Directory for version snapshots (defaults to config.yaml parent / config_versions)
        """
        self.config_path = config_path
        self.versions_dir = versions_dir or (config_path.parent / "config_versions")

        # Ensure versions directory exists
        self.versions_dir.mkdir(parents=True, exist_ok=True)

        # Ensure baseline.yaml exists
        self._ensure_baseline()

    def _ensure_baseline(self) -> None:
        """Ensure a baseline.yaml snapshot exists. Creates one if missing."""
        baseline_path = self.versions_dir / "baseline.yaml"

        if not baseline_path.exists():
            try:
                config = self._load_config(self.config_path)
                with open(baseline_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
            except Exception:
                # Baseline creation is best-effort; don't crash if config is unreadable
                pass

    def _load_config(self, path: Path) -> Dict[str, Any]:
        """
        Load YAML config from disk.

        Args:
            path: Path to YAML file

        Returns:
            Parsed config dict

        Raises:
            FileNotFoundError: If file doesn't exist
            yaml.YAMLError: If YAML is malformed
        """
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _flatten_dict(self, d: Dict[str, Any], parent_key: str = "") -> Dict[str, Any]:
        """
        Flatten nested dict to dotted keys.

        Args:
            d: Nested dictionary
            parent_key: Prefix for recursive calls

        Returns:
            Flattened dict with dotted keys (e.g., "scanner.auto_execute")
        """
        items = {}

        for k, v in d.items():
            new_key = f"{parent_key}.{k}" if parent_key else k

            if isinstance(v, dict):
                items.update(self._flatten_dict(v, new_key))
            else:
                items[new_key] = v

        return items

    def save_version(self, description: str = "") -> int:
        """
        Save a snapshot of the current config.yaml and return its version number.

        Versions are numbered sequentially (v001, v002, ...) with timestamps.
        Files are named: v{NNN}_{YYYYMMDD_HHMMSS}.yaml

        Args:
            description: Optional description for this version

        Returns:
            Version number (e.g., 1, 2, 3, ...)
        """
        # Determine next version number
        version_num = self.get_current_version() + 1

        # Generate timestamped filename
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y%m%d_%H%M%S")
        version_file = self.versions_dir / f"v{version_num:03d}_{ts_str}.yaml"

        # Copy current config to version file
        try:
            config = self._load_config(self.config_path)

            # Add metadata header to version file
            with open(version_file, "w", encoding="utf-8") as f:
                f.write(f"# Version {version_num}\n")
                f.write(f"# Timestamp: {now.isoformat()}\n")
                if description:
                    f.write(f"# Description: {description}\n")
                f.write("# ---\n\n")
                f.write(yaml.safe_dump(config, default_flow_style=False, sort_keys=False))

            return version_num
        except Exception as exc:
            raise RuntimeError(f"Failed to save version {version_num}: {exc}")

    def list_versions(self) -> List[Dict[str, Any]]:
        """
        List all saved configuration versions.

        Returns:
            List of dicts with keys: version (int), timestamp (str), path (str), description (str)
            Sorted newest-first
        """
        versions = []

        if not self.versions_dir.exists():
            return versions

        for yaml_file in sorted(self.versions_dir.glob("v*.yaml")):
            try:
                # Parse filename: v{NNN}_{YYYYMMDD_HHMMSS}.yaml
                stem = yaml_file.stem  # e.g., "v001_20260318_153000"
                parts = stem.split("_")

                if len(parts) >= 3:
                    version_str = parts[0]  # e.g., "v001"
                    version_num = int(version_str[1:])  # Remove 'v' prefix

                    # Extract timestamp from filename
                    ts_part = "_".join(parts[1:])  # Everything after v{NNN}_
                    ts_str = datetime.strptime(ts_part, "%Y%m%d_%H%M%S").isoformat()

                    # Extract description from file header if present
                    description = ""
                    with open(yaml_file, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("# Description:"):
                                description = line.replace("# Description:", "").strip()
                                break
                            if line.startswith("# ---"):
                                break

                    versions.append({
                        "version": version_num,
                        "timestamp": ts_str,
                        "path": str(yaml_file),
                        "description": description,
                    })
            except (ValueError, OSError):
                # Skip unparseable files
                continue

        # Sort newest-first
        return sorted(versions, key=lambda x: x["version"], reverse=True)

    def load_version(self, version_num: int) -> Dict[str, Any]:
        """
        Load a configuration snapshot by version number.

        Args:
            version_num: Version number (1, 2, 3, ...)

        Returns:
            Parsed config dict

        Raises:
            FileNotFoundError: If version doesn't exist
        """
        # Find matching version file
        for yaml_file in self.versions_dir.glob(f"v{version_num:03d}_*.yaml"):
            return self._load_config(yaml_file)

        raise FileNotFoundError(f"Version {version_num} not found")

    def diff_versions(
        self,
        v1: int,
        v2: int,
    ) -> List[Tuple[str, Any, Any]]:
        """
        Compare two configuration versions and return differences.

        Args:
            v1: First version number
            v2: Second version number

        Returns:
            List of (dotted_key, value_in_v1, value_in_v2) tuples
            for all keys where values differ
        """
        config1 = self.load_version(v1)
        config2 = self.load_version(v2)

        flat1 = self._flatten_dict(config1)
        flat2 = self._flatten_dict(config2)

        diffs = []

        # Find keys in v1 or v2 that differ
        all_keys = set(flat1.keys()) | set(flat2.keys())

        for key in sorted(all_keys):
            val1 = flat1.get(key)
            val2 = flat2.get(key)

            if val1 != val2:
                diffs.append((key, val1, val2))

        return diffs

    def prune_old(self, max_versions: int = 50) -> int:
        """
        Delete oldest version files beyond max_versions threshold.

        Keeps the most recent max_versions snapshots.

        Args:
            max_versions: Maximum number of versions to retain

        Returns:
            Number of versions deleted
        """
        versions = self.list_versions()  # Already sorted newest-first

        if len(versions) <= max_versions:
            return 0

        # Delete oldest versions beyond the threshold
        deleted = 0
        for version_info in versions[max_versions:]:
            try:
                Path(version_info["path"]).unlink()
                deleted += 1
            except Exception:
                pass

        return deleted

    def get_current_version(self) -> int:
        """
        Get the highest version number currently saved.

        Returns:
            Highest version number, or 0 if no versions exist
        """
        versions = self.list_versions()
        return versions[0]["version"] if versions else 0

    def restore_to_version(self, version_num: int) -> None:
        """
        Restore config.yaml from a saved version snapshot.

        Overwrites the current config.yaml with the specified version.
        This is a destructive operation; consider backing up first.

        Args:
            version_num: Version number to restore

        Raises:
            FileNotFoundError: If version doesn't exist
            RuntimeError: If restore fails
        """
        config = self.load_version(version_num)

        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        except Exception as exc:
            raise RuntimeError(f"Failed to restore version {version_num}: {exc}")


# Module singleton
_versioner_instance: Optional[ConfigVersioner] = None


def get_config_versioner() -> ConfigVersioner:
    """
    Get or create the singleton ConfigVersioner instance.

    Returns:
        The module-level ConfigVersioner instance
    """
    global _versioner_instance
    if _versioner_instance is None:
        _versioner_instance = ConfigVersioner()
    return _versioner_instance
