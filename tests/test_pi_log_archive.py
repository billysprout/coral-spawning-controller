"""Tests for scripts/pi_log_archive.py (a fake Pi, no ssh) and the Pi's forced command."""
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pi_log_archive as A             # noqa: E402

NOW = dt.datetime(2026, 11, 30, 12, 0, 0, tzinfo=dt.timezone.utc)


def sha(b):
    return hashlib.sha256(b).hexdigest()


class FakePi:
    """Closed segments held in memory: name -> (bytes, closed_at datetime)."""

    def __init__(self, files):
        self.files = dict(files)
        self.removed = []
        self.fail_list = False
        self.corrupt = set()            # names whose fetch returns the wrong bytes
        self.refuse_rm = False

    def list(self):
        if self.fail_list:
            raise A.ArchiveError("ssh list rc=255: no route to host")
        return [(n, len(b), int(t.timestamp())) for n, (b, t) in self.files.items()]

    def sum(self, name):
        return sha(self.files[name][0])

    def fetch(self, name, sink):
        data = self.files[name][0]
        if name in self.corrupt:
            data = data[:-3] + b"???"
        sink(data[:7])
        sink(data[7:])

    def remove(self, name, digest):
        if self.refuse_rm:
            raise A.ArchiveError("ssh rm rc=4: checksum differs")
        assert digest == sha(self.files[name][0])
        del self.files[name]
        self.removed.append(name)


def days_ago(n):
    return NOW - dt.timedelta(days=n)


class PrivateKey(unittest.TestCase):
    def test_the_key_is_copied_to_a_file_only_this_user_can_read(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "id_ed25519")
            with open(src, "wb") as fh:
                fh.write(b"-----BEGIN KEY-----\nabc\n")
            os.chmod(src, 0o644)
            path = A.private_key_copy(src)
            try:
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), b"-----BEGIN KEY-----\nabc\n")
                if os.name != "nt":
                    self.assertEqual(os.stat(path).st_mode & 0o077, 0)
                self.assertNotEqual(path, src)
            finally:
                shutil.rmtree(os.path.dirname(path), ignore_errors=True)


class Archive(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def events(self):
        p = os.path.join(self.dest, "archive.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def test_a_segment_older_than_14_days_is_copied_verified_and_removed_from_the_pi(self):
        body = b'{"ts":"2026-11-10T00:00:00+00:00","lux":1.0}\n' * 50
        pi = FakePi({"lux.jsonl.20261114T000000Z": (body, days_ago(16))})
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertTrue(st["ok"])
        self.assertEqual((st["listed"], st["eligible"], st["archived"]), (1, 1, 1))
        self.assertEqual(pi.removed, ["lux.jsonl.20261114T000000Z"])
        with gzip.open(os.path.join(self.dest, "lux.jsonl.20261114T000000Z.gz")) as fh:
            self.assertEqual(fh.read(), body)
        with open(os.path.join(self.dest, "lux.jsonl.20261114T000000Z.sha256")) as fh:
            self.assertEqual(fh.read().strip(), sha(body))
        self.assertEqual(self.events()[0]["action"], "archived")

    def test_a_younger_segment_stays_on_the_pi(self):
        pi = FakePi({"lux.jsonl.20261120T000000Z": (b"x\n", days_ago(13.9))})
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertEqual((st["listed"], st["eligible"]), (1, 0))
        self.assertEqual(pi.removed, [])
        self.assertEqual(os.listdir(self.dest).count("lux.jsonl.20261120T000000Z.gz"), 0)

    def test_data_already_at_rest_here_is_only_deleted_from_the_pi(self):
        body = b"same bytes\n" * 20
        name = "temp.jsonl.20261101T000000Z"
        with gzip.open(os.path.join(self.dest, name + ".gz"), "wb") as fh:
            fh.write(body)
        with open(os.path.join(self.dest, name + ".sha256"), "w") as fh:
            fh.write(sha(body) + "\n")
        pi = FakePi({name: (body, days_ago(20))})
        fetched = []
        orig = pi.fetch
        pi.fetch = lambda n, s: (fetched.append(n), orig(n, s))
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertEqual((st["already_at_rest"], st["archived"]), (1, 0))
        self.assertEqual(fetched, [])                      # nothing was transferred
        self.assertEqual(pi.removed, [name])

    def test_a_corrupted_transfer_is_never_stored_and_the_pi_copy_stays(self):
        name = "lux.jsonl.20261101T000000Z"
        pi = FakePi({name: (b"abcdefghij" * 30, days_ago(20))})
        pi.corrupt.add(name)
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertFalse(st["ok"])
        self.assertIn("does not match", st["errors"][0])
        self.assertEqual(pi.removed, [])
        self.assertEqual([f for f in os.listdir(self.dest) if f.startswith("lux.jsonl")], [])
        self.assertEqual(self.events()[0]["action"], "error")

    def test_a_refused_delete_keeps_the_local_copy_and_reports(self):
        name = "lux.jsonl.20261101T000000Z"
        pi = FakePi({name: (b"abc\n", days_ago(20))})
        pi.refuse_rm = True
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertFalse(st["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.dest, name + ".gz")))
        self.assertIn(name, pi.files)
        # the next pass finds it already at rest and retries only the delete
        pi.refuse_rm = False
        st2 = A.run_once(pi, self.dest, 14, NOW)
        self.assertEqual((st2["ok"], st2["already_at_rest"]), (True, 1))
        self.assertNotIn(name, pi.files)

    def test_one_bad_segment_does_not_stop_the_others(self):
        pi = FakePi({"lux.jsonl.20261101T000000Z": (b"one\n" * 9, days_ago(25)),
                     "lux.jsonl.20261102T000000Z": (b"two\n" * 9, days_ago(24))})
        pi.corrupt.add("lux.jsonl.20261101T000000Z")
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertEqual((st["archived"], len(st["errors"])), (1, 1))
        self.assertEqual(pi.removed, ["lux.jsonl.20261102T000000Z"])

    def test_an_unreachable_pi_is_a_failed_pass_not_an_exception(self):
        pi = FakePi({})
        pi.fail_list = True
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertFalse(st["ok"])
        self.assertIn("no route", st["errors"][0])
        with open(os.path.join(self.dest, "status.json")) as fh:
            self.assertFalse(json.load(fh)["ok"])

    def test_the_status_reports_the_oldest_segment(self):
        pi = FakePi({"lux.jsonl.20261120T000000Z": (b"x\n", days_ago(10)),
                     "temp.jsonl.20261119T000000Z": (b"y\n", days_ago(11))})
        st = A.run_once(pi, self.dest, 14, NOW)
        self.assertEqual(st["oldest_age_days"], 11.0)


@unittest.skipUnless(shutil.which("bash") and os.name != "nt" or os.environ.get("BASH_FORCED_COMMAND_TEST"),
                     "needs bash with sha256sum and stat (the Pi's)")
class ForcedCommand(unittest.TestCase):
    """The Pi's pi_archive_shell.sh against a temp folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(ROOT, "scripts", "pi_archive_shell.sh"), newline="") as fh:
            src = fh.read()
        src = src.replace("\r\n", "\n").replace("DIR=/home/pi", f"DIR={self.tmp}")
        self.script = os.path.join(self.tmp, "shell.sh")
        with open(self.script, "w", newline="\n") as fh:
            fh.write(src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, name, body=b"data\n", age_s=0):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(body)
        if age_s:
            t = dt.datetime.now().timestamp() - age_s
            os.utime(p, (t, t))
        return p

    def run_cmd(self, command):
        p = subprocess.run(["bash", self.script], capture_output=True, text=True,
                           env=dict(os.environ, SSH_ORIGINAL_COMMAND=command))
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def test_list_shows_only_closed_segments(self):
        self.put("lux.jsonl.20261101T000000Z")
        self.put("temp.jsonl.1")
        self.put("lux.jsonl")                       # live
        self.put("lux.jsonl.bak")
        rc, out, _ = self.run_cmd("list")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(x.split()[0] for x in out.splitlines()),
                         ["lux.jsonl.20261101T000000Z", "temp.jsonl.1"])

    def test_the_live_file_and_odd_names_are_refused(self):
        self.put("lux.jsonl")
        for cmd in ("cat lux.jsonl", "sum lux.jsonl", "cat ../etc/passwd",
                    "cat lux.jsonl.1;id", "rm lux.jsonl " + "0" * 64, "bogus", "cat"):
            rc, _, _ = self.run_cmd(cmd)
            self.assertNotEqual(rc, 0, cmd)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "lux.jsonl")))

    def test_cat_and_sum(self):
        self.put("lux.jsonl.1", b"hello\n")
        self.assertEqual(self.run_cmd("cat lux.jsonl.1")[1], "hello")
        self.assertEqual(self.run_cmd("sum lux.jsonl.1")[1], sha(b"hello\n"))

    def test_rm_needs_the_right_checksum_and_a_day_of_age(self):
        old = self.put("lux.jsonl.2", b"old\n", age_s=3 * 86400)
        new = self.put("lux.jsonl.3", b"new\n", age_s=60)
        self.assertNotEqual(self.run_cmd("rm lux.jsonl.2 " + "0" * 64)[0], 0)      # wrong sum
        self.assertTrue(os.path.exists(old))
        self.assertNotEqual(self.run_cmd("rm lux.jsonl.3 " + sha(b"new\n"))[0], 0)  # too young
        self.assertTrue(os.path.exists(new))
        rc, out, _ = self.run_cmd("rm lux.jsonl.2 " + sha(b"old\n"))
        self.assertEqual((rc, out), (0, "removed"))
        self.assertFalse(os.path.exists(old))


if __name__ == "__main__":
    unittest.main()
