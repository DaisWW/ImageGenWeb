from __future__ import annotations

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
                    {"attempted_channel_ids", "circuit_probe"}.issubset(
                        generation_item_columns
                    )
                )
                self.assertIn("circuit_probe", generation_attempt_columns)
                self.assertIn("channel_circuit_states", inspector.get_table_names())
            finally:
                engine.dispose()
