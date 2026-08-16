from __future__ import annotations

import unittest

from ai_software_factory.graph import topological_order
from ai_software_factory.models import MAX_SPEC_BYTES, FactorySpec, SpecError

from tests.support import spec, task


class ModelTests(unittest.TestCase):
    def test_canonical_round_trip_is_lossless(self):
        parsed = FactorySpec.from_dict(
            spec(
                task(
                    tests=[{"name": "unit", "command": ["python", "-c", "pass"]}],
                    environment={"MODE": "test"},
                    timeout_seconds=1.5,
                    max_attempts=4,
                )
            )
        )
        self.assertEqual(FactorySpec.from_json(parsed.canonical_json()), parsed)

    def test_unknown_root_field_is_rejected(self):
        value = spec()
        value["mystery"] = True
        with self.assertRaisesRegex(SpecError, "unknown field"):
            FactorySpec.from_dict(value)

    def test_non_string_unknown_key_becomes_spec_error(self):
        value = spec()
        value[7] = "bad"  # type: ignore[index]
        with self.assertRaises(SpecError):
            FactorySpec.from_dict(value)

    def test_non_object_root_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "root must be an object"):
            FactorySpec.from_dict([])

    def test_duplicate_json_key_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "duplicate JSON"):
            FactorySpec.from_json('{"schema_version":1,"schema_version":1}')

    def test_specification_input_size_is_bounded_before_parsing(self):
        with self.assertRaisesRegex(SpecError, "exceeds"):
            FactorySpec.from_json(" " * (MAX_SPEC_BYTES + 1))

    def test_boolean_is_not_an_integer(self):
        value = spec()
        value["schema_version"] = True
        with self.assertRaisesRegex(SpecError, "must be an integer"):
            FactorySpec.from_dict(value)

    def test_duplicate_task_ids_are_rejected(self):
        with self.assertRaisesRegex(SpecError, "task ids must be unique"):
            FactorySpec.from_dict(spec(task("same"), task("same")))

    def test_unknown_dependency_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "unknown task"):
            FactorySpec.from_dict(spec(task(depends_on=["missing"])))

    def test_self_dependency_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "cannot depend on itself"):
            FactorySpec.from_dict(spec(task("build", depends_on=["build"])))

    def test_cycle_reports_path(self):
        with self.assertRaisesRegex(SpecError, "dependency cycle detected"):
            FactorySpec.from_dict(
                spec(task("a", depends_on=["b"]), task("b", depends_on=["a"]))
            )

    def test_deep_dag_does_not_recurse(self):
        tasks = []
        for index in range(1_500):
            dependencies = [] if index == 0 else [f"t{index - 1:04d}"]
            tasks.append(task(f"t{index:04d}", depends_on=dependencies))
        parsed = FactorySpec.from_dict(
            spec(*tasks, budget={"max_tasks": 2000, "max_attempts": 3000})
        )
        self.assertEqual(len(topological_order(parsed.tasks)), 1_500)

    def test_exact_path_conflict_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "path ownership conflict"):
            FactorySpec.from_dict(
                spec(task("a", owned_paths=["src/a.py"]), task("b", artifacts=["src/a.py"]))
            )

    def test_parent_child_path_conflict_is_order_independent(self):
        for tasks in (
            (task("a", owned_paths=["src"]), task("b", owned_paths=["src/a.py"])),
            (task("a", owned_paths=["src/a.py"]), task("b", owned_paths=["src"])),
        ):
            with self.subTest(tasks=tasks), self.assertRaisesRegex(
                SpecError, "path ownership conflict"
            ):
                FactorySpec.from_dict(spec(*tasks))

    def test_same_task_may_list_artifact_under_owned_directory(self):
        parsed = FactorySpec.from_dict(
            spec(task(owned_paths=["reports"], artifacts=["reports/result.json"]))
        )
        self.assertEqual(parsed.tasks[0].artifacts, ("reports/result.json",))

    def test_unsafe_paths_are_rejected(self):
        for path in ("/tmp/x", "../x", "a\\b", "a/", "a//b", ".", "safe\x00bad"):
            with self.subTest(path=path), self.assertRaises(SpecError):
                FactorySpec.from_dict(spec(task(owned_paths=[path])))

    def test_non_finite_json_numbers_are_rejected(self):
        with self.assertRaisesRegex(SpecError, "non-finite"):
            FactorySpec.from_json(
                '{"schema_version":1,"name":"x","budget":{"max_wall_seconds":NaN},"tasks":[]}'
            )

    def test_sensitive_environment_name_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "looks sensitive"):
            FactorySpec.from_dict(spec(task(environment={"API_TOKEN": "synthetic"})))

    def test_invalid_environment_key_is_rejected_cleanly(self):
        with self.assertRaises(SpecError):
            FactorySpec.from_dict(spec(task(environment={2: "x"})))

    def test_control_character_in_owner_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "control characters"):
            FactorySpec.from_dict(spec(task(owner="agent\nforged")))

    def test_topological_order_is_deterministic(self):
        parsed = FactorySpec.from_dict(
            spec(
                task("z"),
                task("a"),
                task("last", depends_on=["z", "a"]),
            )
        )
        self.assertEqual(topological_order(parsed.tasks), ("a", "z", "last"))

    def test_task_budget_is_enforced(self):
        with self.assertRaisesRegex(SpecError, "exceeding"):
            FactorySpec.from_dict(
                spec(task("a"), task("b"), budget={"max_tasks": 1})
            )


if __name__ == "__main__":
    unittest.main()
