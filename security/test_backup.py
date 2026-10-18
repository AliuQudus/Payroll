import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from . import backup


def rows(path):
    with closing(sqlite3.connect(path)) as db:
        return sorted(r[0] for r in db.execute("SELECT name FROM people"))


def tree(root):
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class BackupCase(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db, self.media, self.out = self.tmp / "live.sqlite3", self.tmp / "media", self.tmp / "backups"
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE people (name TEXT)")
            db.executemany("INSERT INTO people VALUES (?)", [("Ada",), ("Tunde",)])
            db.commit()
        (self.media / "hr_documents" / "1").mkdir(parents=True)
        (self.media / "hr_documents" / "1" / "contract.pdf").write_bytes(b"%PDF-1.4 contract")
        (self.media / "recruitment" / "1").mkdir(parents=True)
        (self.media / "recruitment" / "1" / "cv.pdf").write_bytes(b"%PDF-1.4 cv")
        self.database = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(self.db)}

    def make(self, **kw):
        return backup.create_backup(self.out, database=self.database, media_root=self.media, **kw)

    def build(self, name, files, extra=None):
        """A hand-made archive, to feed the checks things a real backup would never contain."""
        path = self.tmp / name
        with tarfile.open(path, "w:gz") as archive:
            for arcname, data in files.items():
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if extra:
                extra(archive)
        return path

    def tampered(self, archive, edit):
        """Unpack a real backup, let `edit(folder)` damage it, and pack it up again."""
        work = self.tmp / "unpacked"
        shutil.rmtree(work, ignore_errors=True)
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
        edit(work)
        path = self.tmp / "tampered.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for p in sorted(work.rglob("*")):
                if p.is_file():
                    tar.add(p, arcname=str(p.relative_to(work)))
        return path


class CreateTests(BackupCase):
    def test_an_archive_with_everything_in_it(self):
        path = self.make()
        self.assertTrue(path.name.startswith(backup.PREFIX) and path.name.endswith(backup.SUFFIX))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)             # salaries inside: owner only
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        manifest = backup.verify_archive(path)
        self.assertEqual((manifest["engine"], manifest["database_file"], manifest["format"]),
                         ("sqlite", "database.sqlite3", 1))
        self.assertEqual(sorted(f["path"] for f in manifest["media_files"]),
                         ["hr_documents/1/contract.pdf", "recruitment/1/cv.pdf"])
        self.assertIn("payroll", manifest["migrations"])
        self.assertEqual(os.listdir(self.out), [path.name])                    # no scratch files left behind

    def test_it_works_with_no_media_folder(self):
        shutil.rmtree(self.media)
        self.assertEqual(backup.verify_archive(self.make())["media_files"], [])

    def test_a_backup_taken_during_a_write_holds_only_committed_data(self):
        writer = sqlite3.connect(self.db, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO people VALUES ('Uncommitted')")
        try:
            path = self.make()
        finally:
            writer.execute("COMMIT")
            writer.close()
        with tarfile.open(path) as tar, tempfile.TemporaryDirectory() as work:
            tar.extractall(work, filter="data")
            self.assertEqual(rows(Path(work) / "database.sqlite3"), ["Ada", "Tunde"])

    def test_names_sort_in_the_order_they_were_made(self):
        names = [self.make().name for _ in range(4)]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(set(names)), 4)

    def test_keeping_only_the_newest(self):
        made = [self.make() for _ in range(4)]
        (self.out / "notes.txt").write_text("mine")
        (self.out / "payroll-backup-old.zip").write_text("not ours")          # does not match our pattern exactly
        self.make(keep=2)
        survivors = sorted(p.name for p in self.out.glob("payroll-backup-*.tar.gz"))
        self.assertEqual(len(survivors), 2)
        self.assertIn(made[-1].name, survivors)                                # the newest old one is kept...
        self.assertTrue((self.out / "notes.txt").exists() and (self.out / "payroll-backup-old.zip").exists())
        self.assertNotIn(made[0].name, survivors)

    def test_the_backup_just_made_is_never_the_one_deleted(self):
        for _ in range(3):
            self.make()
        newest = self.make(keep=1)
        self.assertEqual(list(self.out.glob("payroll-backup-*.tar.gz")), [newest])

    def test_a_missing_database_is_reported(self):
        self.db.unlink()
        with self.assertRaises(CommandError):
            self.make()
        self.assertEqual(list(self.out.glob("payroll-backup-*")), [])          # no half-made archive either

    def test_unsupported_engines_are_refused(self):
        with self.assertRaises(CommandError):
            backup.create_backup(self.out, database={"ENGINE": "django.db.backends.mysql", "NAME": "x"},
                                 media_root=self.media)


class VerifyTests(BackupCase):
    def test_damage_is_detected(self):
        archive = self.make()
        garbage = self.tmp / "garbage.tar.gz"
        garbage.write_bytes(b"this is not an archive")
        with self.assertRaises(CommandError):
            backup.verify_archive(garbage)
        truncated = self.tmp / "truncated.tar.gz"
        truncated.write_bytes(archive.read_bytes()[:200])
        with self.assertRaises(CommandError):
            backup.verify_archive(truncated)
        with self.assertRaises(CommandError):
            backup.verify_archive(self.tmp / "does-not-exist.tar.gz")

    def test_a_changed_database_or_media_file_is_detected(self):
        archive = self.make()

        def db(work):
            (work / "database.sqlite3").write_bytes((work / "database.sqlite3").read_bytes() + b"x")

        def media(work):
            (work / "media" / "hr_documents" / "1" / "contract.pdf").write_bytes(b"swapped")

        def gone(work):
            (work / "media" / "recruitment" / "1" / "cv.pdf").unlink()

        for edit, text in ((db, "database"), (media, "contract.pdf"), (gone, "cv.pdf")):
            with self.assertRaises(CommandError) as ctx:
                backup.verify_archive(self.tampered(archive, edit))
            self.assertIn(text, str(ctx.exception))

    def test_a_missing_manifest_or_unknown_format_is_refused(self):
        archive = self.make()

        def no_manifest(work):
            (work / "manifest.json").unlink()

        def future(work):
            data = json.loads((work / "manifest.json").read_text())
            data["format"] = 99
            (work / "manifest.json").write_text(json.dumps(data))

        for edit in (no_manifest, future):
            with self.assertRaises(CommandError):
                backup.verify_archive(self.tampered(archive, edit))

    def test_hostile_archives_are_not_trusted(self):
        outside = self.tmp / "evil.txt"
        symlink = lambda archive: archive.addfile(self._link("media/link", "/etc/passwd"))
        crafted = {
            "path traversal": self.build("a.tar.gz", {"../evil.txt": b"x"}),
            "nested traversal": self.build("b.tar.gz", {"media/../../evil.txt": b"x"}),
            "absolute path": self.build("c.tar.gz", {"/tmp/evil.txt": b"x"}),
            "unknown file": self.build("d.tar.gz", {"manifest.json": b"{}", "surprise.sh": b"rm -rf /"}),
            "symlink": self.build("e.tar.gz", {"manifest.json": b"{}"}, extra=symlink),
        }
        for label, path in crafted.items():
            with self.assertRaises(CommandError, msg=label):
                backup.verify_archive(path)
        self.assertFalse(outside.exists())

    def test_our_own_checks_stop_traversal_even_on_a_python_without_safe_extraction(self):
        """Python 3.12's data filter also blocks these. Take it away, to prove our checks do not depend on it.

        A normal file comes first so that the media folder exists, as a real attacker would arrange: without our
        checks the escape then genuinely succeeds and writes outside the scratch folder.
        """
        escape = Path(tempfile.gettempdir()) / "escaped-by-test.txt"
        escape.unlink(missing_ok=True)
        self.addCleanup(escape.unlink, missing_ok=True)
        crafted = self.build("old.tar.gz", {"media/ok.txt": b"fine", "media/../../escaped-by-test.txt": b"escaped",
                                            "manifest.json": b"{}"})
        with mock.patch("security.backup._has_data_filter", return_value=False):
            with self.assertRaises(CommandError):
                backup.verify_archive(crafted)
            with self.assertRaises(CommandError):
                backup.restore_archive(crafted, str(self.db), str(self.media))
        self.assertFalse(escape.exists(), "the archive wrote a file outside the scratch folder")
        self.assertEqual(rows(self.db), ["Ada", "Tunde"])

    @staticmethod
    def _link(name, target):
        info = tarfile.TarInfo(name)
        info.type, info.linkname = tarfile.SYMTYPE, target
        return info


class RestoreTests(BackupCase):
    def damage_live_data(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("DELETE FROM people")
            db.execute("INSERT INTO people VALUES ('Intruder')")
            db.commit()
        (self.media / "hr_documents" / "1" / "contract.pdf").unlink()
        (self.media / "junk.txt").write_text("added after the backup")

    def test_a_round_trip_brings_back_exactly_what_was_saved(self):
        original = tree(self.media)
        archive = self.make()
        self.damage_live_data()
        info = backup.restore_archive(archive, str(self.db), str(self.media))
        self.assertEqual(rows(self.db), ["Ada", "Tunde"])
        self.assertEqual(tree(self.media), original)                           # the junk file is gone, the lost one is back
        self.assertEqual(info["media_files"], 2)

    def test_what_was_replaced_is_set_aside_not_deleted(self):
        archive = self.make()
        self.damage_live_data()
        info = backup.restore_archive(archive, str(self.db), str(self.media))
        aside = list(self.tmp.glob(f"live.sqlite3{info['set_aside_suffix']}"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(rows(aside[0]), ["Intruder"])
        old_media = Path(str(self.media) + info["set_aside_suffix"])
        self.assertTrue((old_media / "junk.txt").exists())

    def test_restoring_to_a_fresh_place(self):
        archive = self.make()
        new_db, new_media = self.tmp / "new" / "db.sqlite3", self.tmp / "new" / "media"
        new_db.parent.mkdir()
        backup.restore_archive(archive, str(new_db), str(new_media))
        self.assertEqual((rows(new_db), tree(new_media)), (["Ada", "Tunde"], tree(self.media)))

    def test_the_database_alone_can_be_restored(self):
        archive = self.make()
        self.damage_live_data()
        backup.restore_archive(archive, str(self.db), str(self.media), restore_media=False)
        self.assertEqual(rows(self.db), ["Ada", "Tunde"])
        self.assertTrue((self.media / "junk.txt").exists())                    # media left exactly as it was

    def test_a_damaged_archive_changes_nothing(self):
        archive = self.make()
        self.damage_live_data()
        before = (rows(self.db), tree(self.media))
        bad = self.tampered(archive, lambda work: (work / "database.sqlite3").write_bytes(b"corrupt"))
        with self.assertRaises(CommandError):
            backup.restore_archive(bad, str(self.db), str(self.media))
        self.assertEqual((rows(self.db), tree(self.media)), before)
        self.assertEqual(list(self.tmp.glob("*before-restore*")), [])          # it stopped before touching anything

    def test_an_archive_with_no_media_gives_an_empty_media_folder(self):
        shutil.rmtree(self.media)
        archive = self.make()
        backup.restore_archive(archive, str(self.db), str(self.media))
        self.assertTrue(self.media.is_dir())
        self.assertEqual(tree(self.media), {})

    def test_a_postgresql_archive_is_pointed_at_pg_restore(self):
        dump = b"fake dump"
        manifest = {"format": 1, "engine": "postgresql", "database_file": "database.dump",
                    "database_sha256": __import__("hashlib").sha256(dump).hexdigest(), "media_files": [],
                    "created_at": "2026-01-01T00:00:00+00:00", "migrations": {}}
        archive = self.build("pg.tar.gz", {"manifest.json": json.dumps(manifest).encode(), "database.dump": dump})
        with self.assertRaises(CommandError) as ctx:
            backup.restore_archive(archive, str(self.db), str(self.media))
        self.assertIn("pg_restore", str(ctx.exception))
        self.assertEqual(rows(self.db), ["Ada", "Tunde"])


class PostgresDumpTests(BackupCase):
    pg = {"ENGINE": "django.db.backends.postgresql", "NAME": "payroll", "USER": "app", "PASSWORD": "s3cret-pw",
          "HOST": "db.internal", "PORT": "5432"}

    def test_pg_dump_missing_is_explained(self):
        with mock.patch("security.backup.shutil.which", return_value=None):
            with self.assertRaises(CommandError) as ctx:
                backup.create_backup(self.out, database=self.pg, media_root=self.media)
        self.assertIn("pg_dump was not found", str(ctx.exception))

    def test_the_dump_is_requested_without_putting_the_password_on_the_command_line(self):
        def fake_run(command, env, capture_output, text):
            Path(command[command.index("--file") + 1]).write_bytes(b"dump contents")
            return SimpleNamespace(returncode=0, stderr="")

        with mock.patch("security.backup.shutil.which", return_value="/usr/bin/pg_dump"), \
                mock.patch("security.backup.subprocess.run", side_effect=fake_run) as run:
            archive = backup.create_backup(self.out, database=self.pg, media_root=self.media)
        command, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertNotIn("s3cret-pw", " ".join(command))                       # anyone can read the process list
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "s3cret-pw")
        for expected in ("--host", "db.internal", "--port", "5432", "--username", "app", "payroll", "--format=custom"):
            self.assertIn(expected, command)
        self.assertEqual(backup.verify_archive(archive)["engine"], "postgresql")

    def test_a_failed_dump_reports_the_reason(self):
        with mock.patch("security.backup.shutil.which", return_value="/usr/bin/pg_dump"), \
                mock.patch("security.backup.subprocess.run",
                           return_value=SimpleNamespace(returncode=1, stderr="connection refused")):
            with self.assertRaises(CommandError) as ctx:
                backup.create_backup(self.out, database=self.pg, media_root=self.media)
        self.assertIn("connection refused", str(ctx.exception))
        self.assertNotIn("s3cret-pw", str(ctx.exception))


class CommandTests(BackupCase):
    def run_backup(self, **kw):
        out = io.StringIO()
        fake = SimpleNamespace(DATABASES={"default": self.database}, MEDIA_ROOT=str(self.media),
                               BACKUP_DIR=str(self.out))
        with mock.patch("security.backup.settings", fake), \
                mock.patch("security.management.commands.backup.settings", fake):
            call_command("backup", stdout=out, **kw)
        return out.getvalue()

    def run_restore(self, archive, **kw):
        out = io.StringIO()
        fake = SimpleNamespace(DATABASES={"default": self.database}, MEDIA_ROOT=str(self.media))
        with mock.patch("security.management.commands.restore.settings", fake):
            call_command("restore", str(archive), stdout=out, **kw)
        return out.getvalue()

    def test_backup_command(self):
        text = self.run_backup(output_dir=str(self.out), keep=1)
        self.assertIn("Backup written", text)
        self.assertIn("verified", text)
        self.assertIn("personal data", text)
        self.assertNotIn("verified", self.run_backup(output_dir=str(self.out), no_verify=True))

    def test_restore_command_needs_an_explicit_yes(self):
        archive = self.make()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("DELETE FROM people")
            db.commit()
        with self.assertRaises(CommandError) as ctx:
            self.run_restore(archive)
        self.assertIn("--yes", str(ctx.exception))
        self.assertEqual(rows(self.db), [])                                    # untouched without confirmation
        text = self.run_restore(archive, yes=True)
        self.assertIn("Restored the backup", text)
        self.assertIn(".before-restore-", text)
        self.assertEqual(rows(self.db), ["Ada", "Tunde"])

    def test_restore_command_refuses_a_missing_or_foreign_archive(self):
        with self.assertRaises(CommandError):
            self.run_restore(self.tmp / "nothing.tar.gz", yes=True)

    def test_restore_command_is_for_sqlite_only(self):
        archive = self.make()
        self.database = {"ENGINE": "django.db.backends.postgresql", "NAME": "x"}
        with self.assertRaises(CommandError) as ctx:
            self.run_restore(archive, yes=True)
        self.assertIn("pg_restore", str(ctx.exception))
