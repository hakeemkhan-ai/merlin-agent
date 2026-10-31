"""Container worker health depends on its own fresh presence, not another worker."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deploy.worker_health import healthy
from merlin.persistence import SQLTaskStore


class DeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.heartbeat = self.root / "heartbeat"

    def test_worker_health_requires_its_own_fresh_presence(self) -> None:
        ready = Mock(return_value={"ready": True})
        self.assertFalse(healthy(self.heartbeat, max_age_seconds=30, readiness=ready))
        ready.assert_not_called()
        self.heartbeat.touch()
        timestamp = self.heartbeat.stat().st_mtime
        self.assertTrue(
            healthy(
                self.heartbeat,
                max_age_seconds=30,
                readiness=ready,
                timestamp=timestamp,
            )
        )
        self.assertFalse(
            healthy(
                self.heartbeat,
                max_age_seconds=30,
                readiness=ready,
                timestamp=timestamp + 31,
            )
        )
        self.assertFalse(
            healthy(
                self.heartbeat,
                max_age_seconds=30,
                readiness=ready,
                timestamp=timestamp - 1,
            )
        )

    def test_worker_health_checks_database_metadata_without_loading_tasks(self) -> None:
        store = SQLTaskStore("sqlite:///:memory:")
        self.addCleanup(store.close)
        store.create_schema()
        self.heartbeat.touch()
        with patch.object(store, "get_task", side_effect=AssertionError("No task load allowed")):
            self.assertFalse(
                healthy(
                    self.heartbeat,
                    max_age_seconds=30,
                    readiness=store.readiness,
                )
            )
            store.worker_heartbeat("another-container")
            self.assertTrue(
                healthy(
                    self.heartbeat,
                    max_age_seconds=30,
                    readiness=store.readiness,
                )
            )
            self.heartbeat.unlink()
            self.assertFalse(
                healthy(
                    self.heartbeat,
                    max_age_seconds=30,
                    readiness=store.readiness,
                )
            )

    def test_worker_health_fails_closed_on_database_and_schema_errors(self) -> None:
        self.heartbeat.touch()
        unavailable = Mock(side_effect=RuntimeError("private database details"))
        self.assertFalse(
            healthy(
                self.heartbeat,
                max_age_seconds=30,
                readiness=unavailable,
            )
        )
        unavailable.assert_called_once_with(30)

    def test_worker_health_rejects_symlink_and_nonregular_markers(self) -> None:
        ready = Mock(return_value={"ready": True})
        target = self.root / "other-marker"
        target.touch()
        self.heartbeat.symlink_to(target)
        self.assertFalse(healthy(self.heartbeat, max_age_seconds=30, readiness=ready))
        self.heartbeat.unlink()
        os.mkfifo(self.heartbeat)
        self.assertFalse(healthy(self.heartbeat, max_age_seconds=30, readiness=ready))
        ready.assert_not_called()
