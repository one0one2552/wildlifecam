"""
storage_manager.py — Local storage guard + NAS upload

Flow: Local Save → Verify NAS Upload → Delete Local File
NAS uploads run in a background ThreadPoolExecutor thread.
"""

from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import psutil

logger = logging.getLogger(__name__)


class StorageManager:
    """
    Manages local /recordings directory and optional NAS upload via SMB.
    """

    def __init__(self, config: dict) -> None:
        self._apply_config(config)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nas-upload")
        # Persistent upload status: filename -> "queued" | "uploaded" | "failed"
        self._upload_status: dict[str, str] = {}

    def _apply_config(self, config: dict) -> None:
        storage = config.get("storage", {})
        self._recordings_path = Path(storage.get("recordings_path", "/recordings"))
        self._min_free_mb: int = storage.get("min_free_mb", 500)
        self._halt_free_mb: int = storage.get("halt_free_mb", 100)
        nas = storage.get("nas", {})
        self._nas_enabled: bool = nas.get("enabled", False)
        self._nas_server: str = nas.get("server", "")
        self._nas_share: str = nas.get("share", "")
        self._nas_user: str = nas.get("username", "")
        self._nas_password: str = nas.get("password", "")
        self._nas_remote_path: str = nas.get("remote_path", "/")

    def update_config(self, config: dict) -> None:
        self._apply_config(config)

    # ------------------------------------------------------------------ #
    # Disk checks                                                          #
    # ------------------------------------------------------------------ #

    def free_mb(self) -> float:
        usage = psutil.disk_usage(str(self._recordings_path))
        return usage.free / (1024 * 1024)

    def is_storage_critical(self) -> bool:
        return self.free_mb() < self._halt_free_mb

    def is_storage_low(self) -> bool:
        return self.free_mb() < self._min_free_mb

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def handle_new_recording(self, file_path: str) -> None:
        """
        Called after a recording is saved locally.
        Schedules NAS upload in background if enabled.
        """
        path = Path(file_path)
        if not path.exists():
            logger.error("Recording file not found: %s", file_path)
            return

        free = self.free_mb()
        if free < self._min_free_mb:
            logger.warning("Disk low: %.0f MB free (threshold %d MB)", free, self._min_free_mb)

        if self._nas_enabled:
            self._upload_status[path.name] = "queued"
            self._executor.submit(self._upload_and_verify, path)
            # Upload PIR graph sidecar JPEG if present
            jpg_sidecar = path.with_suffix(".jpg")
            if jpg_sidecar.exists():
                self._executor.submit(self._upload_and_verify, jpg_sidecar)
        else:
            logger.info("NAS disabled — keeping local: %s", file_path)

    def list_recordings(self) -> list[dict]:
        """Return metadata for all local MP4 recordings, newest first."""
        recordings = []
        for p in sorted(
            self._recordings_path.glob("*.mp4"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        ):
            stat = p.stat()
            recordings.append({
                "filename": p.name,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "mtime": stat.st_mtime,
                "url": f"/recordings/{p.name}",
                "upload_status": self._upload_status.get(p.name),
            })
        return recordings

    def list_nas_recordings(self) -> list[dict]:
        """Return list of MP4 files on the NAS share, newest first."""
        if not self._nas_enabled or not self._nas_server or not self._nas_share:
            return []
        try:
            import smbclient  # type: ignore
            smbclient.register_session(
                self._nas_server,
                username=self._nas_user,
                password=self._nas_password,
                connection_timeout=5,
            )
            nas_remote = self._nas_remote_path.replace("/", "\\")
            remote_dir = f"\\\\{self._nas_server}\\{self._nas_share}{nas_remote}"
            entries = []
            for entry in smbclient.scandir(remote_dir):
                if not entry.name.lower().endswith(".mp4"):
                    continue
                stat = entry.stat()
                entries.append({
                    "filename": entry.name,
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "mtime": stat.st_mtime,
                })
            entries.sort(key=lambda e: e["mtime"], reverse=True)
            return entries
        except Exception:
            logger.exception("Failed to list NAS recordings")
            raise

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------ #
    # NAS upload                                                           #
    # ------------------------------------------------------------------ #

    def request_upload(self, filename: str) -> bool:
        """Queue a manual on-demand upload of a single file. Local copy is kept."""
        path = self._recordings_path / filename
        if not path.exists():
            return False
        self._upload_status[filename] = "queued"
        self._executor.submit(self._upload_and_verify, path, False)
        return True

    def _upload_and_verify(self, local_path: Path, delete_local: bool = True) -> None:
        """
        Background task: upload file to SMB NAS, verify, then optionally delete local copy.
        On any failure the local file is retained for retry on the next cycle.
        """
        try:
            import smbclient  # type: ignore  # from smbprotocol
            import smbclient.path  # type: ignore

            smbclient.register_session(
                self._nas_server,
                username=self._nas_user,
                password=self._nas_password,
            )

            nas_remote = self._nas_remote_path.replace("/", "\\")
            remote_dir = f"\\\\{self._nas_server}\\{self._nas_share}{nas_remote}"
            remote_file = f"{remote_dir}\\{local_path.name}"

            logger.info("NAS upload starting: %s → %s", local_path.name, remote_file)

            with open(local_path, "rb") as f_local:
                with smbclient.open_file(remote_file, mode="wb") as f_remote:
                    shutil.copyfileobj(f_local, f_remote)

            # Verify: compare file sizes
            local_size = local_path.stat().st_size
            remote_size = smbclient.stat(remote_file).st_size
            if local_size != remote_size:
                raise ValueError(
                    f"Size mismatch: local={local_size} remote={remote_size}"
                )

            self._upload_status[local_path.name] = "uploaded"
            if delete_local:
                logger.info("NAS upload verified (%d bytes). Deleting local copy.", local_size)
                local_path.unlink()
            else:
                logger.info("NAS upload verified (%d bytes). Local copy retained.", local_size)

        except Exception:
            self._upload_status[local_path.name] = "failed"
            logger.exception("NAS upload failed for %s — local file retained.", local_path.name)
