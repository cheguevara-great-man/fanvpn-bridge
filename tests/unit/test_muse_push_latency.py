"""Clock-bound arithmetic used by the explicit read-only latency diagnostic."""
import importlib.util
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("muse_push_latency_test", Path(__file__).resolve().parents[2] /
                                             "tools/muse_push/latency.py")
latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(latency)
return_spec = importlib.util.spec_from_file_location("muse_return_latency_test", Path(__file__).resolve().parents[2] /
                                                    "tools/muse_push/return_latency.py")
return_latency = importlib.util.module_from_spec(return_spec)
with patch.dict(sys.modules, {"latency": latency}):
    return_spec.loader.exec_module(return_latency)


class ClockBoundsTests(unittest.TestCase):
    def test_intersection_does_not_assume_symmetric_network_paths(self):
        samples = [{"low_ms": -1100, "high_ms": -700}, {"low_ms": -1000, "high_ms": -800}]
        self.assertEqual(latency.clock_bounds(samples), (-1000, -800))

    def test_conflicting_samples_are_not_reported_as_precise_timings(self):
        with self.assertRaises(ValueError):
            latency.clock_bounds([{"low_ms": -1000, "high_ms": -800}, {"low_ms": -700, "high_ms": -600}])

    def test_pc_to_remote_subtracts_offset(self):
        result = latency.corrected_interval(100, (-1000, -800), remote_received=True)
        self.assertEqual(result, {"estimate_seconds": 1.0, "bounds_seconds": [0.9, 1.1]})

    def test_remote_to_pc_adds_offset(self):
        result = latency.corrected_interval(1500, (-1000, -800), remote_received=False)
        self.assertEqual(result, {"estimate_seconds": 0.6, "bounds_seconds": [0.5, 0.7]})

    def test_near_zero_bounds_are_clamped_but_impossible_clocks_fail(self):
        result = latency.corrected_interval(850, (-1000, -800), remote_received=False)
        self.assertEqual(result["bounds_seconds"], [0, 0.05])
        with self.assertRaises(ValueError):
            latency.corrected_interval(500, (-1000, -800), remote_received=False)


class ReturnTimingTests(unittest.TestCase):
    def test_instrumented_copy_compiles_and_timestamps_before_parsing_and_logging(self):
        source = (Path(__file__).resolve().parents[2] / "tools/muse_push/scripts/pc.py").read_text(encoding="utf-8")
        timed = return_latency.timed_source(source)
        ast.parse(timed)
        self.assertLess(timed.index("raw = response.read"), timed.index("received_ms = time.time_ns"))
        self.assertLess(timed.index("received_ms = time.time_ns"), timed.index("result = json.loads(raw"))
        self.assertLess(timed.index("received_ms = time.time_ns"), timed.index('trace.open("x"'))
        self.assertIn('json.dump({"call_id": result["call_id"], "received_ms": received_ms,', timed)
        self.assertIn('"connection_generation": _opener.generation', timed)

    def test_unrecognized_or_ambiguous_source_is_not_silently_instrumented(self):
        for source in ("", "        raw = response.read(MAX_BYTES + 1)\n        return result\n" * 2):
            with self.assertRaises(ValueError):
                return_latency.timed_source(source)


if __name__ == "__main__":
    unittest.main()
