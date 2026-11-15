"""CLI delegates to verified storage tools without logging database secrets."""

from unittest.mock import patch

import manage


def test_db_backup_uses_private_environment_and_never_initializes_cluster(tmp_path, capsys):
    # Break caught: backup accidentally initializes a second local PostgreSQL cluster.
    destination = tmp_path / "backup"
    with patch.dict("os.environ", {"QUANT_DATABASE_URL": "postgresql://u:secret@localhost/db"}), \
            patch("manage.load_environment"), \
            patch("tools.database_backup.backup_database",
                  return_value=destination / "manifest.json") as backup, \
            patch("manage.ensure_local_postgres", side_effect=AssertionError("second DB")):
        assert manage.main(["db", "backup", "--output", str(destination)]) == 0
    assert backup.call_args.args == ("postgresql://u:secret@localhost/db", destination)
    assert "secret" not in capsys.readouterr().out


def test_db_restore_reads_target_env_and_preserves_environment_dsn(tmp_path, capsys):
    # Break caught: restore accepts production connection as an implicit target.
    target = "postgresql://u:restore-secret@localhost/quant_restore_drill"
    with patch.dict("os.environ", {"QUANT_DRILL_DSN": target}), \
            patch("manage.load_environment"), \
            patch("tools.database_backup.restore_database",
                  return_value={"status": "restored"}) as restore:
        assert manage.main(["db", "restore", "--backup", str(tmp_path),
                            "--target-dsn-env", "QUANT_DRILL_DSN"]) == 0
    assert restore.call_args.args == (tmp_path, target)
    assert "restore-secret" not in capsys.readouterr().out


def test_storage_cli_forwards_relative_paths_without_implicit_apply():
    # Break caught: read-only inventory unexpectedly executes storage cutover.
    with patch("manage.load_environment"), \
            patch("tools.storage_migration.main", return_value=0) as storage:
        assert manage.main(["storage", "precopy", "logs"]) == 0
    assert storage.call_args.args == (["precopy", "logs"],)


def test_default_backup_refuses_unavailable_configured_disk_before_writing(tmp_path):
    # Break caught: missing external disk silently writes backups to internal root.
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "missing-disk/quant-data"
    env = {"QUANT_DATABASE_URL": "postgresql://u@localhost/db",
           "QUANT_DATA_ROOT": str(external), "QUANT_DATA_VOLUME_UUID": "expected",
           "QUANT_DATA_MOUNT": str(external.parent)}
    with patch.dict("os.environ", env), patch("manage.ROOT", project), \
            patch("manage.load_environment"), \
            patch("tools.storage_migration.probe_volume",
                  return_value={"mounted": False, "uuid": None}), \
            patch("tools.database_backup.backup_database",
                  side_effect=AssertionError("backup wrote despite absent disk")):
        assert manage.main(["db", "backup"]) == 1
    assert not external.exists()
    assert not (project / "database/backups").exists()
