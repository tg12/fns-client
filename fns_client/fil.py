# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""SFTP client for obtaining FNS initial load files."""

from __future__ import annotations

import gzip
import io
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fns_client.config import FilConfig

LOGGER = logging.getLogger(__name__)


class FilClient:
    """Download FIL payloads from the SWIM SFTP endpoint."""

    def __init__(self, config: FilConfig) -> None:
        self._config = config
        self._ssh_client = None
        self._sftp_client = None

    def connect(self) -> None:
        """Open the SSH and SFTP sessions if they are not already open."""

        # Import paramiko lazily so replay mode does not require SFTP dependencies.
        import paramiko

        if self._sftp_client is not None:
            return
        ssh_client = paramiko.SSHClient()
        if self._config.strict_host_key_checking.lower() == "yes":
            ssh_client.load_host_keys(self._config.known_hosts_file_path)
            ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = paramiko.RSAKey.from_private_key_file(self._config.cert_file_path)
        ssh_client.connect(
            hostname=self._config.host,
            port=self._config.port,
            username=self._config.username,
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )
        self._ssh_client = ssh_client
        self._sftp_client = ssh_client.open_sftp()
        LOGGER.info(
            "Connected to FIL SFTP host=%s port=%s",
            self._config.host,
            self._config.port,
        )

    def close(self) -> None:
        """Close open SFTP resources."""

        if self._sftp_client is not None:
            self._sftp_client.close()
            self._sftp_client = None
        if self._ssh_client is not None:
            self._ssh_client.close()
            self._ssh_client = None

    @contextmanager
    def open_initial_load(self, ref_datetime: datetime) -> Iterator[io.BufferedReader]:
        """Yield the current FIL payload as a decompressed binary stream."""

        self.connect()
        assert self._sftp_client is not None

        remote_date_path = self._config.fil_date_file_name
        remote_file_path = self._config.fil_file_name
        while True:
            with self._sftp_client.open(remote_date_path, mode="r") as handle:
                raw_timestamp = handle.read().strip()
            mod_time = datetime.strptime(
                f"{raw_timestamp} UTC",
                self._config.fil_data_file_time_format,
            )
            if mod_time >= ref_datetime:
                LOGGER.info("Found FIL snapshot modified=%s", mod_time.isoformat())
                break
            LOGGER.info(
                "Waiting for a FIL snapshot newer than %s", ref_datetime.isoformat()
            )
            time.sleep(60)

        if self._config.fil_file_save_path:
            output_dir = Path(self._config.fil_file_save_path)
            output_dir.mkdir(parents=True, exist_ok=True)
            local_path = output_dir / Path(remote_file_path).name
            self._sftp_client.get(remote_file_path, str(local_path))
            with gzip.open(local_path, mode="rb") as handle:
                yield handle
            return

        with (
            self._sftp_client.open(remote_file_path, mode="rb") as remote_handle,
            gzip.GzipFile(fileobj=remote_handle, mode="rb") as handle,
        ):
            yield handle
