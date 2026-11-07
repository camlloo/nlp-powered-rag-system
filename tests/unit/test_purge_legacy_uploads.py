"""Mock-only tests; no database, uploaded file, or application settings are accessed."""

import ctypes
import datetime as dt
import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import purge_legacy_uploads as purge

CUTOFF = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
OLD = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
NEW = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
URI_A = "uploads/" + "a" * 32 + ".txt"
URI_B = "uploads/" + "b" * 32 + ".pdf"
SECRET = "12" * 32
DIGEST = b"x" * 32
ROOT = Path("D:/mock-storage")


def version(doc, version_id, uri, created=OLD):
    return {
        "document_id": doc, "document_title": f"doc-{doc}", "deleted_at": None,
        "version_id": version_id, "version_no": 1, "storage_uri": uri,
        "created_at": created, "parse_status": "succeeded",
    }


def reference(row, kb=1):
    return {"version_id": row["version_id"], "document_id": row["document_id"],
            "kb_id": kb, "storage_uri": row["storage_uri"]}


def index(idx, kb=1, manifest=None, status="ready"):
    return {"id": idx, "kb_id": kb, "status": status, "source_revision": 3,
            "manifest_json": manifest if manifest is not None else {"files": []}}


def chunk(cid, idx, doc, vid, embedding=True):
    return {"id": cid, "index_id": idx, "document_id": doc, "doc_version_id": vid,
            "has_embedding": embedding}


def kb():
    return {"id": 1, "tenant_id": 1, "name": "mock", "active_index_id": 10,
            "data_revision": 3, "acl_revision": 2, "deleted_at": None}


def inventory(versions, **overrides):
    kwargs = {"kb": kb(), "versions": versions, "all_uris": [reference(v) for v in versions],
              "indexes": [], "chunks": [], "jobs": [], "active_refs": [], "citations": [],
              "qa_requests": [], "root": ROOT, "cutoff": CUTOFF}
    kwargs.update(overrides)
    with mock.patch.object(purge, "inspect_file", return_value={"state": "regular_file"}):
        result = purge.build_inventory(**kwargs)
    result["active_index"] = {"id": 10, "kb_id": 1, "status": "ready"}
    return result


class Cursor:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.calls = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        self.rowcount = 1

    def fetchall(self):
        return next(self.responses)

    def fetchone(self):
        return next(self.responses)


class Connection:
    def __init__(self, responses=()):
        self.cur = Cursor(responses)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class SelectionTests(unittest.TestCase):
    def test_strict_uris_and_exclusive_cutoff(self):
        self.assertTrue(purge.trusted_upload_uri(URI_A))
        for uri in ["uploads/../file.txt", "s3://bucket/file", URI_A.upper(),
                    URI_A + ":stream", URI_A + "\n", "uploads/old.txt", None]:
            self.assertFalse(purge.trusted_upload_uri(uri))
        self.assertEqual(purge.parse_cutoff("2026-01-01T08:00:00+08:00"), CUTOFF)
        with self.assertRaises(purge.argparse.ArgumentTypeError):
            purge.parse_cutoff("2026-01-01")
        selected, skipped = purge.select_versions([
            version(1, 1, URI_A), version(1, 2, URI_B, NEW),
            version(2, 3, URI_A), version(2, 4, "s3://bucket/x"),
            version(3, 5, URI_A, CUTOFF), version(4, 6, URI_B),
            version(5, 7, "uploads/old.txt"),
        ], CUTOFF)
        self.assertEqual([v["version_id"] for v in selected], [6])
        self.assertEqual([d["document_id"] for d in skipped], [1, 2, 5])

    def test_preview_flags_mixed_indexes_jobs_and_retained_audit(self):
        v1, v2 = version(1, 1, URI_A), version(2, 2, URI_B, NEW)
        report = inventory(
            [v1, v2], indexes=[index(10, manifest={"files": [1, 2]}),
                               index(11, manifest={"unknown": True})],
            chunks=[chunk(100, 10, 1, 1), chunk(101, 10, 2, 2)],
            jobs=[{"id": 1, "kb_id": 1, "doc_version_id": 1, "status": "succeeded"},
                  {"id": 2, "kb_id": 1, "doc_version_id": None, "status": "queued"}],
            active_refs=[{"id": 1, "active_index_id": 10}],
            citations=[{"id": 7, "qa_request_id": 8, "doc_version_id": 1, "chunk_id": 100}],
            qa_requests=[{"id": 8, "kb_id": 1, "index_id": 10}],
        )
        self.assertEqual(report["documents"][0]["versions"][0]["id"], 1)
        mixed = report["indexes_referencing_targets_or_unknown"][0]
        self.assertEqual(mixed["other_chunk_count"], 1)
        self.assertEqual(mixed["other_chunks"][0]["id"], 101)
        self.assertEqual(mixed["other_manifest_version_ids"], [2])
        self.assertEqual(mixed["active_for_kb_ids"], [1])
        self.assertEqual(len(report["relevant_jobs"]), 2)
        self.assertEqual(report["retained_qa_citations"][0]["id"], 7)
        self.assertTrue(any("审计" in warning for warning in report["warnings"]))
        self.assertEqual(purge.preview_digest(report), purge.preview_digest(report))
        self.assertNotEqual(
            purge.preview_digest(report), purge.preview_digest({**report, "kb": {}})
        )

    def test_preflight_rejects_all_active_tasks_and_bad_indexes(self):
        for status in ("queued", "running"):
            report = inventory([version(1, 1, URI_A)], jobs=[
                {"id": 9, "kb_id": 1, "doc_version_id": None, "status": status}])
            with self.assertRaises(purge.UnsafePurge):
                purge._preflight(report)
        for item in [index(1, manifest={"invalid": []}),
                     index(2, kb=2, manifest={"files": [1]}),
                     index(3, status="building", manifest={"files": [1]})]:
            with self.assertRaises(purge.UnsafePurge):
                purge._preflight(inventory([version(1, 1, URI_A)], indexes=[item]))

    def test_unknown_or_cross_kb_active_index_blocks(self):
        report = inventory([version(1, 1, URI_A)])
        for active in (None, {"id": 10, "kb_id": 2, "status": "ready"},
                       {"id": 10, "kb_id": 1, "status": "building"}):
            with self.subTest(active=active), self.assertRaises(purge.UnsafePurge):
                purge._preflight({**report, "active_index": active})

    def test_manifest_only_removes_target_and_refuses_unknown_shapes(self):
        manifest = {"files": [1, 2], "chunk_counts": {"1": 4, "2": 7},
                    "totals": {"chunks": 11}}
        self.assertEqual(purge._safe_manifest(manifest, {1}),
                         {"files": [2], "chunk_counts": {"2": 7}})
        for bad in [{"files": [1], "unrecognized": {}}, {"files": [True]},
                    {"files": [1], "chunk_counts": {"1": True}}]:
            with self.assertRaises(purge.UnsafePurge):
                purge._safe_manifest(bad, {1})

    def test_unknown_local_path_blocks_but_s3_does_not(self):
        self.assertEqual(purge._referenced(URI_A, [URI_A]), "shared")
        self.assertEqual(purge._referenced(URI_A, ["uploads/../different"]), "unknown_reference")
        self.assertIsNone(purge._referenced(URI_A, [URI_B, "s3://nlprag/object"]))


class DatabaseTests(unittest.TestCase):
    def test_read_only_inventory_uses_repeatable_read(self):
        v = version(1, 1, URI_A)
        conn = Connection([[kb()], [v], [reference(v)], [index(10, manifest={"files": [1]})],
                           [{"id": 100, "index_id": 10}], [chunk(100, 10, 1, 1)],
                           [], [{"id": 1, "active_index_id": 10}], [], []])
        with mock.patch.object(purge, "inspect_file", return_value={"state": "missing"}):
            report = purge.collect_inventory(conn, 1, ROOT, CUTOFF)
        self.assertEqual(conn.cur.calls[0][0],
                         "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        self.assertTrue(all(q.startswith("SELECT") for q, _ in conn.cur.calls[3:]))
        self.assertEqual(report["documents"][0]["id"], 1)

    def test_apply_locks_journals_fk_order_and_preserves_other_chunks(self):
        report = inventory([version(1, 1, URI_A)],
                           indexes=[index(10, manifest={"files": [1, 2],
                                                        "chunk_counts": {"1": 1, "2": 1}})],
                           chunks=[chunk(100, 10, 1, 1), chunk(101, 10, 2, 2)],
                           active_refs=[{"id": 1, "active_index_id": 10}])
        conn = Connection([{"id": 1}, [{"id": 1}],
                           [{"id": 1, "storage_uri": URI_A, "file_sha256": DIGEST}],
                           [{"storage_uri": "s3://nlprag/other"}], {"n": 1},
                           [{"id": 10, "manifest_json": {"files": [1, 2],
                                                           "chunk_counts": {"1": 1, "2": 1}}}],
                           None, {"id": 77}])
        with mock.patch.object(purge, "collect_inventory", return_value=report), \
             mock.patch.object(purge, "delete_windows_file", return_value="verified") as file:
            result = purge.apply_database(
                conn, 1, ROOT, CUTOFF, purge.preview_digest(report), "h", SECRET
            )
        self.assertEqual(result["reindex_job_id"], 77)
        file.assert_called_once_with(ROOT, URI_A, DIGEST, delete=False)
        statements = [sql for sql, _ in conn.cur.calls]
        self.assertTrue(any("LOCK TABLE" in sql and "EXCLUSIVE MODE" in sql for sql in statements))
        self.assertTrue(any("FOR UPDATE" in sql and "kb_document" in sql for sql in statements))
        def order(prefix):
            return next(i for i, s in enumerate(statements) if s.startswith(prefix))

        self.assertLess(order("DELETE FROM kb_embedding"), order("DELETE FROM kb_chunk"))
        self.assertLess(order("UPDATE ingest_job"), order("DELETE FROM kb_document_version"))
        self.assertTrue(all("qa_" not in sql and "app_user" not in sql for sql in statements))
        chunk_sql = next(sql for sql in statements if sql.startswith("DELETE FROM kb_chunk"))
        self.assertIn("doc_version_id = ANY", chunk_sql)
        self.assertEqual(conn.commits, 0)

    def test_mismatched_preview_and_active_job_do_not_delete(self):
        report = inventory([version(1, 1, URI_A)])
        for digest, current in [("0" * 64, report),
                                (purge.preview_digest(report), {**report, "relevant_jobs": [
                                    {"id": 2, "kb_id": 1, "doc_version_id": None,
                                     "status": "running"}]})]:
            if current is not report:
                digest = purge.preview_digest(current)
            conn = Connection([{"id": 1}])
            with mock.patch.object(purge, "collect_inventory", return_value=current), \
                 mock.patch.object(purge, "delete_windows_file") as file:
                with self.assertRaises(purge.UnsafePurge):
                    purge.apply_database(conn, 1, ROOT, CUTOFF, digest, "h", SECRET)
            file.assert_not_called()
            self.assertFalse(any(s.startswith("DELETE") for s, _ in conn.cur.calls))

    def test_no_survivors_does_not_enqueue_reindex(self):
        report = inventory([version(1, 1, URI_A)])
        conn = Connection([{"id": 1}, [{"id": 1}],
                           [{"id": 1, "storage_uri": URI_A, "file_sha256": DIGEST}],
                           [], {"n": 0}, [], None])
        with mock.patch.object(purge, "collect_inventory", return_value=report), \
             mock.patch.object(purge, "delete_windows_file", return_value="verified"):
            result = purge.apply_database(
                conn, 1, ROOT, CUTOFF, purge.preview_digest(report), "h", SECRET
            )
        self.assertIsNone(result["reindex_job_id"])
        self.assertFalse(any(s.startswith("INSERT INTO ingest_job") for s, _ in conn.cur.calls))

    def test_unaffected_active_index_does_not_enqueue_reindex(self):
        report = inventory([version(1, 1, URI_A)])
        conn = Connection([{"id": 1}, [{"id": 1}],
                           [{"id": 1, "storage_uri": URI_A, "file_sha256": DIGEST}],
                           [{"storage_uri": "s3://nlprag/other"}], {"n": 1}, [], None])
        with mock.patch.object(purge, "collect_inventory", return_value=report), \
             mock.patch.object(purge, "delete_windows_file", return_value="verified"):
            result = purge.apply_database(
                conn, 1, ROOT, CUTOFF, purge.preview_digest(report), "h", SECRET
            )
        self.assertIsNone(result["reindex_job_id"])
        self.assertFalse(any(s.startswith("INSERT INTO ingest_job") for s, _ in conn.cur.calls))

    def test_surviving_local_version_blocks_rebuild(self):
        report = inventory([version(1, 1, URI_A)])
        conn = Connection([{"id": 1}, [{"id": 1}],
                           [{"id": 1, "storage_uri": URI_A, "file_sha256": DIGEST}],
                           [{"storage_uri": URI_B}]])
        with mock.patch.object(purge, "collect_inventory", return_value=report), \
             mock.patch.object(purge, "delete_windows_file", return_value="verified"):
            with self.assertRaises(purge.UnsafePurge):
                purge.apply_database(
                    conn, 1, ROOT, CUTOFF, purge.preview_digest(report), "h", SECRET
                )
        self.assertFalse(any(s.startswith("DELETE") for s, _ in conn.cur.calls))


class ResumeTests(unittest.TestCase):
    def rows(self, *entries):
        if not entries:
            entries = ((URI_A, "pending"),)
        manifest = purge._manifest_digest({uri: DIGEST for uri, _ in entries})
        result = []
        for uri, state in entries:
            row = {"kb_id": 1, "storage_root": str(Path(os.path.abspath(ROOT))), "uri": uri,
                   "file_sha256": DIGEST, "state": state, "reindex_job_id": 77,
                   "run_size": len(entries), "manifest_sha256": manifest}
            row["record_mac"] = purge._record_mac(SECRET, "h", row)
            result.append(row)
        return result

    def test_shared_file_is_not_deleted_and_remains_retryable(self):
        rows = self.rows()
        conn = Connection([{"name": "journal"}, rows, rows[0], [], [{"storage_uri": URI_A}]])
        with mock.patch.object(purge, "delete_windows_file") as delete, \
             redirect_stdout(io.StringIO()):
            result = purge.resume_files(conn, "h", SECRET, 1, ROOT)
        delete.assert_not_called()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["files"][0]["state"], "shared")
        self.assertEqual(conn.commits, 1)

    def test_deleted_file_and_missing_retry_after_commit_uncertainty(self):
        for file_result in ("deleted", "missing"):
            rows = self.rows()
            conn = Connection([{"name": "journal"}, rows, rows[0], [], []])
            file_patch = mock.patch.object(purge, "delete_windows_file", return_value=file_result)
            with file_patch as delete, redirect_stdout(io.StringIO()):
                result = purge.resume_files(conn, "h", SECRET, 1, ROOT)
            delete.assert_called_once_with(ROOT, URI_A, DIGEST)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["reindex_job_id"], 77)
            self.assertEqual(conn.commits, 1)

    def test_file_or_commit_failure_is_reported_and_can_retry(self):
        rows = self.rows()
        conn = Connection([{"name": "journal"}, rows, rows[0], [], []])
        conn.commit = mock.Mock(side_effect=RuntimeError("secret should not be printed"))
        with mock.patch.object(purge, "delete_windows_file", return_value="deleted"), \
             redirect_stdout(io.StringIO()):
            result = purge.resume_files(conn, "h", SECRET, 1, ROOT)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["files"][0]["state"], "failed_or_unrecorded")
        self.assertEqual(result["files"][0]["error"], "RuntimeError")
        self.assertGreaterEqual(conn.rollbacks, 2)

    def test_changed_or_incomplete_journal_is_refused(self):
        rows = self.rows((URI_A, "pending"), (URI_B, "deleted"))
        for candidate in ([rows[0]], [{**rows[0], "state": "deleted"}, rows[1]]):
            conn = Connection([{"name": "journal"}, candidate])
            with mock.patch.object(purge, "delete_windows_file") as delete:
                with self.assertRaises(purge.UnsafePurge):
                    purge.resume_files(conn, "h", SECRET, 1, ROOT)
            delete.assert_not_called()

    def test_conflicting_jobs_block_resume(self):
        rows = self.rows()
        conn = Connection([{"name": "journal"}, rows, rows[0], [{"id": 99, "status": "running"}]])
        with mock.patch.object(purge, "delete_windows_file") as delete, \
             redirect_stdout(io.StringIO()):
            result = purge.resume_files(conn, "h", SECRET, 1, ROOT)
        delete.assert_not_called()
        self.assertEqual(result["status"], "incomplete")

    def test_unknown_reference_fails_closed_and_already_completed_is_reported(self):
        rows = self.rows((URI_A, "pending"), (URI_B, "deleted"))
        conn = Connection([{"name": "journal"}, rows, rows[0], [],
                           [{"storage_uri": "../unknown"}]])
        with mock.patch.object(purge, "delete_windows_file") as delete, \
             redirect_stdout(io.StringIO()):
            result = purge.resume_files(conn, "h", SECRET, 1, ROOT)
        delete.assert_not_called()
        self.assertEqual(result["files"][0]["state"], "failed_or_unrecorded")
        self.assertTrue(result["files"][1]["previously_recorded"])


@unittest.skipUnless(os.name == "nt", "Windows handle interface")
class WindowsHandleTests(unittest.TestCase):
    def fake_kernel(self, *, reparse=False, links=1):
        kernel = mock.Mock()
        kernel.GetDriveTypeW.return_value = 3
        target_handle = 0
        handles = []

        def create(path, *args):
            nonlocal target_handle
            handle = len(handles) + 1
            handles.append(handle)
            if path.endswith(".txt"):
                target_handle = handle
            return handle

        def information(handle, pointer):
            info = pointer._obj
            info.attributes = 0 if handle == target_handle else 0x10
            if reparse and handle == 2:
                info.attributes |= 0x400
            info.links = links if handle == target_handle else 1
            return 1

        chunks = iter([b"payload", b""])

        def read(handle, buf, length, count, overlapped):
            data = next(chunks)
            ctypes.memmove(buf, data, len(data))
            count._obj.value = len(data)
            return 1

        kernel.CreateFileW.side_effect = create
        kernel.GetFileInformationByHandle.side_effect = information
        kernel.ReadFile.side_effect = read
        kernel.SetFileInformationByHandle.return_value = 1
        return kernel

    def test_verified_handle_and_delete_are_distinct(self):
        digest = hashlib.sha256(b"payload").digest()
        for deleting in (False, True):
            kernel = self.fake_kernel()
            with mock.patch("ctypes.WinDLL", return_value=kernel):
                result = purge.delete_windows_file(ROOT, URI_A, digest, delete=deleting)
            self.assertEqual(result, "deleted" if deleting else "verified")
            self.assertEqual(kernel.SetFileInformationByHandle.call_count, int(deleting))
            self.assertEqual(kernel.CloseHandle.call_count, kernel.CreateFileW.call_count)
            self.assertTrue(all(c.args[2] & 4 == 0 for c in kernel.CreateFileW.call_args_list))

    def test_reparse_hardlinks_and_digest_mismatch_fail_closed(self):
        for kernel in (self.fake_kernel(reparse=True), self.fake_kernel(links=2),
                       self.fake_kernel()):
            with mock.patch("ctypes.WinDLL", return_value=kernel):
                with self.assertRaises(purge.UnsafePurge):
                    purge.delete_windows_file(ROOT, URI_A, b"z" * 32)
            kernel.SetFileInformationByHandle.assert_not_called()
            self.assertEqual(kernel.CloseHandle.call_count, kernel.CreateFileW.call_count)


class CliTests(unittest.TestCase):
    def args(self):
        return ["--kb-id", "1", "--storage-dir", "D:/mock-storage",
                "--created-before", "2026-01-01T00:00:00Z"]

    def test_maintenance_confirmation_required_before_connect(self):
        with mock.patch("psycopg.connect") as connect, redirect_stderr(io.StringIO()):
            result = purge.main(self.args() + ["--apply"])
        self.assertEqual(result, 2)
        connect.assert_not_called()

    def test_commit_failure_does_not_attempt_physical_deletion(self):
        conn = Connection()
        conn.commit = mock.Mock(side_effect=RuntimeError("credentials"))
        prepared = {"status": "prepared", "file_uris": [URI_A]}
        env = {"LEGACY_PURGE_DSN": "fake", "LEGACY_PURGE_RECOVERY_SECRET": SECRET}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("psycopg.connect", return_value=conn), \
             mock.patch.object(purge, "apply_database", return_value=prepared), \
             mock.patch.object(purge, "resume_files") as resume, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            result = purge.main(self.args() + ["--apply", "--maintenance-confirmed",
                                               "--preview-sha256", "1" * 64])
        self.assertEqual(result, 1)
        resume.assert_not_called()
        self.assertIn("可能不确定", errors.getvalue())
        self.assertNotIn("credentials", errors.getvalue())
        self.assertTrue(conn.closed)

    def test_apply_commits_before_file_stage_and_reports_incomplete(self):
        conn = Connection()
        prepared = {"status": "prepared", "file_uris": [URI_A], "reindex_job_id": None}
        env = {"LEGACY_PURGE_DSN": "fake", "LEGACY_PURGE_RECOVERY_SECRET": SECRET}

        def resume(*args):
            self.assertEqual(conn.commits, 1)
            return {"status": "incomplete", "files": [{"uri": URI_A, "state": "shared"}]}

        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("psycopg.connect", return_value=conn), \
             mock.patch.object(purge, "apply_database", return_value=prepared), \
             mock.patch.object(purge, "resume_files", side_effect=resume), \
             redirect_stdout(io.StringIO()) as output:
            result = purge.main(self.args() + ["--apply", "--maintenance-confirmed",
                                               "--preview-sha256", "1" * 64])
        self.assertEqual(result, 1)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([e["event"] for e in events],
                         ["database_prepared", "database_committed", "summary"])
        self.assertEqual(events[-1]["files"][0]["state"], "shared")

    def test_preview_uses_explicit_dsn_and_closes(self):
        conn = Connection()
        report = inventory([version(1, 1, URI_A)])
        with mock.patch.dict(os.environ, {"LEGACY_PURGE_DSN": "fake"}, clear=True), \
             mock.patch("psycopg.connect", return_value=conn), \
             mock.patch.object(purge, "collect_inventory", return_value=report), \
             redirect_stdout(io.StringIO()) as output:
            result = purge.main(self.args())
        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(output.getvalue())["preview_sha256"], purge.preview_digest(report)
        )
        self.assertEqual(conn.rollbacks, 1)
        self.assertTrue(conn.closed)


if __name__ == "__main__":
    unittest.main()
