import unittest

from scripts.websocket_load_test import (
    ViewerResult,
    health_url_for,
    percentile,
    summarize,
)


class WebSocketLoadTestTests(unittest.TestCase):
    def test_percentile_interpolates_and_empty_is_none(self):
        self.assertEqual(percentile([], 95), None)
        self.assertEqual(percentile([10, 20, 30], 50), 20)
        self.assertEqual(percentile([0, 100], 95), 95)

    def test_health_url_uses_matching_transport(self):
        self.assertEqual(
            health_url_for("wss://optionsniper.xyz/ws/prices"),
            "https://optionsniper.xyz/api/health",
        )
        self.assertEqual(
            health_url_for("ws://127.0.0.1:8876/ws/prices"),
            "http://127.0.0.1:8876/api/health",
        )

    def test_summary_requires_all_viewers_latency_and_sequence_continuity(self):
        results = [ViewerResult(
            connected=True,
            snapshots=1,
            deltas=10,
            symbol_updates=100,
            snapshot_bytes=1000,
            delta_bytes=5000,
            latencies_ms=[10, 15, 20],
        ) for _ in range(3)]
        summary = summarize(
            results=results,
            duration=10,
            requested_viewers=3,
            minimum_deltas=8,
            max_p95_latency_ms=250,
            before_stream={"delta_broadcasts": 2, "fanout_messages": 6},
            after_stream={
                "delta_broadcasts": 12,
                "fanout_messages": 36,
                "fanout_bytes": 15_000,
                "send_failures": 0,
                "max_batch_symbols": 10,
            },
        )
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["server"]["delta_broadcasts"], 10)
        self.assertEqual(summary["connected_viewers"], 3)

        results[0].sequence_gaps = 1
        self.assertFalse(summarize(
            results=results,
            duration=10,
            requested_viewers=3,
            minimum_deltas=8,
            max_p95_latency_ms=250,
            before_stream={},
            after_stream={},
        )["passed"])


if __name__ == "__main__":
    unittest.main()
