from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TestMigrationCompatibility(unittest.TestCase):
    def test_old_chat_model_configuration_and_selection_are_removed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "chat-models.sqlite"
            database_url = f"sqlite:///{database_path.as_posix()}"
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
            config.set_main_option("sqlalchemy.url", database_url)

            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                command.upgrade(config, "1f3a9c7e2b64")

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text("""
                    INSERT INTO users (id, username, display_name, password_hash, role, status,
                        balance_rmb, reserved_rmb, password_version, created_at, updated_at)
                    VALUES (1, 'admin', 'Admin', 'hash', 'admin', 'active', 0, 0, 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """)
                )
                connection.execute(
                    text("""
                    INSERT INTO workspaces (id, user_id, name, kind, position, settings,
                        created_at, updated_at)
                    VALUES ('workspace', 1, 'Studio', 'image', 0,
                        '{"chat_model_id":"old-id","prompt":"keep"}',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """)
                )
                connection.execute(
                    text("""
                    INSERT INTO system_state (key, value, updated_at)
                    VALUES ('runtime_config.chat_models.v1', '{}', CURRENT_TIMESTAMP)
                """)
                )
            engine.dispose()

            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                command.upgrade(config, "head")
                command.check(config)

            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    old = connection.scalar(
                        text(
                            "SELECT value FROM system_state "
                            "WHERE key = 'runtime_config.chat_models.v1'"
                        )
                    )
                    settings = json.loads(
                        connection.scalar(
                            text("SELECT settings FROM workspaces WHERE id = 'workspace'")
                        )
                    )
                self.assertIsNone(old)
                self.assertEqual(settings, {"prompt": "keep"})
            finally:
                engine.dispose()

    def test_generation_limits_are_removed_and_restored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "generation-limits.sqlite"
            database_url = f"sqlite:///{database_path.as_posix()}"
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
            config.set_main_option("sqlalchemy.url", database_url)

            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                command.upgrade(config, "b7c8d9e0f1a2")
                command.upgrade(config, "head")

            engine = create_engine(database_url)
            inspector = inspect(engine)
            try:
                self.assertNotIn(
                    "generation_concurrency",
                    {column["name"] for column in inspector.get_columns("users")},
                )
                self.assertNotIn("generation_queue_state", inspector.get_table_names())
                self.assertNotIn(
                    "uq_generation_jobs_workspace_active",
                    {index["name"] for index in inspector.get_indexes("generation_jobs")},
                )
            finally:
                engine.dispose()

            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                command.downgrade(config, "b7c8d9e0f1a2")

            engine = create_engine(database_url)
            inspector = inspect(engine)
            try:
                self.assertIn(
                    "generation_concurrency",
                    {column["name"] for column in inspector.get_columns("users")},
                )
                self.assertIn("generation_queue_state", inspector.get_table_names())
                self.assertIn(
                    "uq_generation_jobs_workspace_active",
                    {index["name"] for index in inspector.get_indexes("generation_jobs")},
                )
            finally:
                engine.dispose()

            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                command.upgrade(config, "head")

            engine = create_engine(database_url)
            inspector = inspect(engine)
            try:
                self.assertNotIn(
                    "generation_concurrency",
                    {column["name"] for column in inspector.get_columns("users")},
                )
                self.assertNotIn("generation_queue_state", inspector.get_table_names())
                self.assertNotIn(
                    "uq_generation_jobs_workspace_active",
                    {index["name"] for index in inspector.get_indexes("generation_jobs")},
                )
            finally:
                engine.dispose()

    def test_legacy_generation_merge_is_repaired(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "legacy.sqlite"
            database_url = f"sqlite:///{database_path.as_posix()}"
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
            config.set_main_option("sqlalchemy.url", database_url)

            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                command.upgrade(config, "c3d4e5f6a7b8")

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE generation_items DROP COLUMN attempted_channel_ids")
                )
                connection.execute(text("ALTER TABLE generation_items DROP COLUMN circuit_probe"))
                connection.execute(
                    text("ALTER TABLE generation_attempts DROP COLUMN circuit_probe")
                )
                connection.execute(text("DROP TABLE channel_circuit_states"))
                connection.execute(
                    text(
                        "CREATE INDEX ix_workspaces_user_position ON workspaces (user_id, position)"
                    )
                )
            engine.dispose()

            with patch.dict(os.environ, {"DATABASE_URL": database_url}):
                command.upgrade(config, "head")

            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                generation_item_columns = {
                    column["name"] for column in inspector.get_columns("generation_items")
                }
                generation_attempt_columns = {
                    column["name"] for column in inspector.get_columns("generation_attempts")
                }
                self.assertTrue(
                    {"attempted_channel_ids", "circuit_probe"}.issubset(generation_item_columns)
                )
                self.assertIn("circuit_probe", generation_attempt_columns)
                self.assertIn("channel_circuit_states", inspector.get_table_names())
                workspace_indexes = {index["name"] for index in inspector.get_indexes("workspaces")}
                self.assertNotIn("ix_workspaces_user_position", workspace_indexes)
            finally:
                engine.dispose()
