from __future__ import annotations

import unittest

from ai_software_factory.state import (
    InvalidTransition,
    RunState,
    TaskState,
    validate_run_transition,
    validate_task_transition,
)


class StateMachineTests(unittest.TestCase):
    def test_happy_task_transitions_are_explicit(self):
        validate_task_transition(TaskState.PENDING, TaskState.READY)
        validate_task_transition(TaskState.READY, TaskState.RUNNING)
        validate_task_transition(TaskState.RUNNING, TaskState.SUCCEEDED)

    def test_terminal_task_cannot_restart(self):
        with self.assertRaises(InvalidTransition):
            validate_task_transition(TaskState.SUCCEEDED, TaskState.RUNNING)

    def test_retry_must_pass_through_ready(self):
        validate_task_transition(TaskState.RUNNING, TaskState.RETRY_WAIT)
        validate_task_transition(TaskState.RETRY_WAIT, TaskState.READY)
        with self.assertRaises(InvalidTransition):
            validate_task_transition(TaskState.RETRY_WAIT, TaskState.RUNNING)

    def test_run_cannot_skip_running(self):
        with self.assertRaises(InvalidTransition):
            validate_run_transition(RunState.CREATED, RunState.SUCCEEDED)

    def test_running_run_can_be_cancelled(self):
        validate_run_transition(RunState.RUNNING, RunState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
