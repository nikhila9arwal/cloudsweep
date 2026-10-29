"""Manifest, command, sizing and cost rules (no AWS, no network)."""

import json
import unittest
from decimal import Decimal

from helpers import TYPES, core


class CommandTests(unittest.TestCase):
    def test_placeholders_substitute_and_literal_braces_survive(self):
        argv = ["{bin}", "--out={out}", "--seed={index}", "--trace", "{input:data/t.bin}", "{{x}}"]
        values = {"bin": "/work/src/build/sim", "out": "/work/out", "src": "/work/src", "index": "3"}
        self.assertEqual(core.substitute(argv, values, {"data/t.bin": "0" * 64}),
                         ["/work/src/build/sim", "--out=/work/out", "--seed=3", "--trace",
                          "/work/src/data/t.bin", "{x}"])

    def test_unknown_or_unsubmitted_placeholders_are_refused(self):
        for argv in (["{nope}"], ["{input:missing}"], ["a{b"], []):
            with self.assertRaises(ValueError):
                core.check_command(argv, set())

    def test_bin_needs_a_binary_in_the_recipe(self):
        with self.assertRaisesRegex(ValueError, "binary"):
            core.check_command(["{bin}"], set(), has_binary=False)
        core.check_command(["python3", "sim.py", "{out}"], set(), has_binary=False)

    def test_manifest_lines_skip_comments_and_report_line_numbers(self):
        text = '# sweep\n["{bin}", "a"]\n\n["{bin}", "b"]\n'
        self.assertEqual(core.parse_commands(text), [["{bin}", "a"], ["{bin}", "b"]])
        with self.assertRaisesRegex(ValueError, "line 2"):
            core.parse_commands('["ok"]\n["{bad}"]\n')

    def test_input_names_stay_inside_the_checkout(self):
        for name in ("../x", "/abs", "a/../b", ".git/config", "a//b", ""):
            with self.assertRaises(ValueError):
                core.input_name(name)
        self.assertEqual(core.input_name("data/run 1/x.bin"), "data/run 1/x.bin")


class RecipeAndManifestTests(unittest.TestCase):
    RECIPE = {"command": "make -j", "binary": "build/sim"}

    def test_build_key_changes_with_commit_base_and_recipe(self):
        commit, base = "a" * 40, "sha256:" + "b" * 64
        key = core.build_key(commit, base, self.RECIPE)
        self.assertNotEqual(key, core.build_key("c" * 40, base, self.RECIPE))
        self.assertNotEqual(key, core.build_key(commit, "sha256:" + "d" * 64, self.RECIPE))
        self.assertNotEqual(key, core.build_key(commit, base, dict(self.RECIPE, command="make")))

    def test_submission_manifest_round_trips_and_rejects_tampering(self):
        manifest = core.submission_manifest("20261029T000000Z-abcdef", "a" * 40, "sha256:" + "b" * 64,
                                            "sha256:" + "c" * 64, self.RECIPE, {"x.bin": "d" * 64},
                                            [["{bin}"]], 1, 3500, Decimal("1.5"))
        self.assertEqual(core.validate_submission(json.loads(core.pretty(manifest))), manifest)
        for field, value in (("commit", "short"), ("recipe", {"command": 1, "binary": None}),
                             ("resources", {"vcpus": 0, "memory_mib": 3500, "hours": "1"})):
            with self.assertRaises(ValueError):
                core.validate_submission(dict(manifest, **{field: value}))

    def test_time_limits(self):
        self.assertEqual(core.timeout_seconds("0.5"), 1800)
        for bad in ("0", "nan", "x", str(24 * 15)):
            with self.assertRaises(ValueError):
                core.timeout_seconds(bad)


class SizingTests(unittest.TestCase):
    def test_cheapest_fitting_instance_is_chosen(self):
        self.assertEqual(core.instance_for(TYPES, 1, 3500)[0], "m7a.large")
        self.assertEqual(core.instance_for(TYPES, 1, 8000)[0], "m7a.xlarge")   # 7372 MiB placeable on large
        self.assertEqual(core.instance_for(TYPES, 3, 1000)[0], "m7a.xlarge")
        with self.assertRaisesRegex(ValueError, "no listed instance fits"):
            core.instance_for(TYPES, 64, 1000)

    def test_cost_is_the_whole_instance_for_the_time_limit(self):
        self.assertEqual(core.job_cost(TYPES, 1, 3500, Decimal(2)), Decimal("0.23184"))
        estimate = core.estimate(TYPES, 10, 1, 3500, Decimal(2))
        self.assertEqual((estimate["instance"], estimate["run_each_usd"], estimate["total_estimate_usd"]),
                         ("m7a.large", "0.23", "2.32"))

    def test_instance_types_are_validated(self):
        for bad in ({}, {"m7a.large": {"vcpus": 2, "memory_mib": 8192, "usd_per_hour": "0"}},
                    {"bad": {"vcpus": 2, "memory_mib": 8192, "usd_per_hour": "1"}}):
            with self.assertRaises(ValueError):
                core.check_instance_types(bad)


class AttemptTests(unittest.TestCase):
    def test_attempt_names_never_reuse_a_directory(self):
        self.assertEqual(core.next_attempt_name("1", set()), "1")
        self.assertEqual(core.next_attempt_name("1", {"1", "1-2"}), "1-3")
        self.assertEqual(sorted(["2", "1-2", "1"], key=core.attempt_order), ["1", "1-2", "2"])


if __name__ == "__main__":
    unittest.main()
