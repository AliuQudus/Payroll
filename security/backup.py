"""Backing up and restoring the database and the private media folder (HR documents and CVs).

An archive holds a manifest, the database and the media files. SQLite is snapshotted with its own backup API, so a
backup taken while people are using the app is still consistent; PostgreSQL is dumped with pg_dump.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import django
from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

PREFIX = "payroll-backup-"
SUFFIX = ".tar.gz"
MANIFEST = "manifest.json"
SQLITE_FILE, POSTGRES_FILE = "database.sqlite3", "database.dump"
FORMAT = 1


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def engine_of(database=None):
    name = (database or settings.DATABASES["default"])["ENGINE"]
    return "sqlite" if name.endswith("sqlite3") else "postgresql" if name.endswith("postgresql") else name


# ---------------------------------------------------------------- creating
def _dump_sqlite(source_path, dest):
    if not os.path.exists(source_path):
        raise CommandError(f"The database file {source_path} does not exist.")
    with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(dest)) as target:
        source.backup(target)                           # consistent even while the app is writing


def _dump_postgres(database, dest):
    if shutil.which("pg_dump") is None:
        raise CommandError("pg_dump was not found. Install the PostgreSQL client tools, or back up the database "
                           "another way and keep the media folder with it.")
    env = {**os.environ, "PGPASSWORD": database.get("PASSWORD") or ""}
    command = ["pg_dump", "--format=custom", "--no-owner", "--file", dest, "--dbname", database["NAME"]]
    for flag, key in (("--host", "HOST"), ("--port", "PORT"), ("--username", "USER")):
        if database.get(key):
            command += [flag, str(database[key])]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(f"pg_dump failed: {result.stderr.strip()}")


def _media_files(media_root):
    root = Path(media_root)
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _migration_state():
    state = {}
    for app, name in MigrationRecorder(connection).applied_migrations():
        state[app] = max(state.get(app, ""), name)
    return state


def create_backup(output_dir, keep=None, verify=True, database=None, media_root=None):
    """Write a new archive into output_dir and return its path."""
    database = database or settings.DATABASES["default"]
    media_root = media_root or settings.MEDIA_ROOT
    engine = engine_of(database)
    if engine not in ("sqlite", "postgresql"):
        raise CommandError(f"Backups are not supported for the {engine} database engine.")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    # fixed-width, down to the microsecond, so names sort in the order they were made and never collide
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    final = output_dir / f"{PREFIX}{stamp}{SUFFIX}"
    if final.exists():
        raise CommandError(f"{final.name} already exists.")
    with tempfile.TemporaryDirectory(dir=output_dir) as work:
        db_name = SQLITE_FILE if engine == "sqlite" else POSTGRES_FILE
        db_copy = os.path.join(work, db_name)
        (_dump_sqlite(database["NAME"], db_copy) if engine == "sqlite" else _dump_postgres(database, db_copy))
        files = _media_files(media_root)
        manifest = {
            "format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(), "django": django.get_version(),
            "engine": engine, "database_file": db_name, "database_sha256": sha256(db_copy),
            "media_files": [{"path": str(p.relative_to(media_root)), "sha256": sha256(p)} for p in files],
            "migrations": _migration_state()}
        manifest_path = os.path.join(work, MANIFEST)
        with open(manifest_path, "w") as handle:
            json.dump(manifest, handle, indent=1)
        partial = output_dir / (final.name + ".partial")
        with tarfile.open(partial, "w:gz") as archive:
            archive.add(manifest_path, arcname=MANIFEST)
            archive.add(db_copy, arcname=db_name)
            for path in files:
                archive.add(path, arcname="media/" + str(path.relative_to(media_root)).replace(os.sep, "/"))
        os.chmod(partial, 0o600)                                # salaries and personal data: owner only
        os.replace(partial, final)
    if verify:
        verify_archive(final)
    if keep:
        prune(output_dir, keep, protect=final)
    return final


def prune(output_dir, keep, protect=None):
    """Delete the oldest of our own archives beyond the newest `keep`. Nothing else in the folder is touched."""
    archives = sorted(Path(output_dir).glob(f"{PREFIX}*{SUFFIX}"))
    for old in archives[:max(len(archives) - keep, 0)]:
        if old != protect:
            old.unlink()


# ---------------------------------------------------------------- checking and reading
def _safe_members(archive):
    """Only the files a backup can contain, with no way to write outside the target folder."""
    allowed, members = {MANIFEST, SQLITE_FILE, POSTGRES_FILE}, []
    for member in archive.getmembers():
        parts = Path(member.name).parts
        if member.isdir():
            continue
        if (not member.isreg() or member.name.startswith("/") or ".." in parts
                or not (member.name in allowed or (parts and parts[0] == "media" and len(parts) > 1))):
            raise CommandError(f"The archive contains an unexpected entry ({member.name}), so it was not trusted.")
        members.append(member)
    return members


def _has_data_filter():
    """Newer Pythons can refuse unsafe paths themselves. Our own checks in _safe_members do not rely on that."""
    return hasattr(tarfile, "data_filter")


def _extract(archive, members, target):
    kwargs = {"filter": "data"} if _has_data_filter() else {}
    archive.extractall(target, members=members, **kwargs)


def verify_archive(path):
    """Check that an archive is whole and its database opens. Returns the manifest."""
    try:
        with tarfile.open(path, "r:gz") as archive, tempfile.TemporaryDirectory() as work:
            members = _safe_members(archive)
            _extract(archive, members, work)
            if not os.path.exists(os.path.join(work, MANIFEST)):
                raise CommandError("The archive has no manifest, so it is not one of ours.")
            with open(os.path.join(work, MANIFEST)) as handle:
                manifest = json.load(handle)
            if manifest.get("format") != FORMAT:
                raise CommandError(f"Unknown backup format {manifest.get('format')}.")
            db_path = os.path.join(work, manifest["database_file"])
            if not os.path.exists(db_path) or sha256(db_path) != manifest["database_sha256"]:
                raise CommandError("The database in the archive is missing or damaged.")
            if manifest["engine"] == "sqlite":
                with closing(sqlite3.connect(db_path)) as db:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise CommandError("The database in the archive fails SQLite's integrity check.")
            for entry in manifest["media_files"]:
                stored = os.path.join(work, "media", entry["path"])
                if not os.path.exists(stored) or sha256(stored) != entry["sha256"]:
                    raise CommandError(f"The file {entry['path']} in the archive is missing or damaged.")
            return manifest
    except (tarfile.TarError, EOFError, OSError, json.JSONDecodeError, KeyError) as exc:
        raise CommandError(f"The archive could not be read ({type(exc).__name__}: {exc}).")


# ---------------------------------------------------------------- restoring
def _aside(path, stamp):
    """Move an existing file or folder out of the way, never deleting it."""
    if os.path.exists(path):
        os.rename(path, f"{path}.before-restore-{stamp}")


def restore_archive(path, db_path, media_root, restore_media=True):
    """Put an archive's database (SQLite) and media back. What was there is renamed, not deleted."""
    manifest = verify_archive(path)
    if manifest["engine"] != "sqlite":
        raise CommandError("This archive holds a PostgreSQL dump. Restore it with pg_restore, then copy the media "
                           "folder back by hand (see DEPLOY.md).")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    with tarfile.open(path, "r:gz") as archive, tempfile.TemporaryDirectory(dir=os.path.dirname(db_path) or ".") as work:
        _extract(archive, _safe_members(archive), work)
        for leftover in ("", "-wal", "-shm"):                  # a half-written journal from the old database
            _aside(db_path + leftover, stamp)
        shutil.copy2(os.path.join(work, manifest["database_file"]), db_path)
        if restore_media:
            _aside(str(media_root), stamp)
            source = os.path.join(work, "media")
            shutil.copytree(source, media_root) if os.path.isdir(source) else os.makedirs(media_root)
    return {"created_at": manifest["created_at"], "media_files": len(manifest["media_files"]),
            "set_aside_suffix": f".before-restore-{stamp}"}
