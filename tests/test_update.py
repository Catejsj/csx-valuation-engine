import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from src import update


class UpdateStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CSX_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_first_start_is_due(self):
        self.assertTrue(update.due())

    def test_recent_update_is_not_due_old_one_is(self):
        update._write({"state": "done", "finished": datetime.now().isoformat()})
        self.assertFalse(update.due())
        update._write({"state": "done", "finished": (datetime.now() - timedelta(days=8)).isoformat()})
        self.assertTrue(update.due())

    def test_running_job_is_not_started_twice_but_a_dead_one_is(self):
        update._write({"state": "running"})
        self.assertTrue(update.running())
        self.assertFalse(update.due())
        st = update.read_status()
        st["heartbeat"] = (datetime.now() - timedelta(hours=1)).isoformat()
        update.status_path().write_text(__import__("json").dumps(st), encoding="utf-8")
        self.assertFalse(update.running())
        self.assertTrue(update.due())

    def test_offline_keeps_the_app_working_and_tries_again(self):
        from src.collect import csx
        with mock.patch.object(update, "reachable", return_value=True), \
                mock.patch.object(csx, "sync", side_effect=csx.SourceUnavailable("timed out")):
            self.assertEqual(update.run(log=lambda *_: None), 3)
        st = update.read_status()
        self.assertEqual(st["state"], "offline")
        self.assertTrue(update.due())          # no finished update yet, so the next start tries again

    def test_unreachable_site_gives_up_quickly(self):
        from src.collect import csx
        with mock.patch.object(update, "reachable", return_value=False), mock.patch.object(csx, "sync") as sync:
            self.assertEqual(update.run(log=lambda *_: None), 3)
        sync.assert_not_called()
        self.assertEqual(update.read_status()["state"], "offline")


if __name__ == "__main__":
    unittest.main()
