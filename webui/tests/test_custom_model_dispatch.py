import unittest
from unittest import mock

from studio.app import jobs


class CustomModelDispatchTests(unittest.TestCase):
    def test_custom_models_keep_separate_bounded_queue_keys(self):
        with mock.patch.object(jobs, "_load_deck", return_value={"model": "custom:7"}):
            self.assertEqual(jobs._model_key(17), "custom:7")

    def test_unbounded_dispatch_does_not_require_builtin_model_queues(self):
        loop = mock.Mock()
        with (
            mock.patch.object(jobs, "_loop", loop),
            mock.patch.object(jobs, "_queues", {}),
            mock.patch.object(jobs, "MAX_PER_MODEL", 0),
        ):
            jobs.enqueue(17)

        loop.call_soon_threadsafe.assert_called_once_with(
            jobs._schedule_unbounded, 17
        )


if __name__ == "__main__":
    unittest.main()
