import threading
import unittest
from unittest.mock import Mock

from core.long_horizon_agent import LongHorizonAgent


class PlannerTests(unittest.TestCase):
    def agent(self, decisions, output="Done"):
        agent = LongHorizonAgent.__new__(LongHorizonAgent)
        agent.context = {"cancel_event": threading.Event()}
        agent.max_steps = 3
        agent.logger = Mock()
        agent.registry = Mock()
        agent.registry.has.return_value = True
        agent.registry.run.return_value = output
        agent._plan = Mock(side_effect=decisions)
        return agent

    def test_no_success_without_tool_evidence(self):
        agent = self.agent([{"done": True}])
        self.assertFalse(agent.run("finish")['ok'])
        agent.registry.run.assert_not_called()

    def test_failure_cannot_become_verified_completion(self):
        agent = self.agent([{"tool": "click"}, {"done": True}], '{"ok":false}')
        result = agent.run("click")
        self.assertFalse(result["ok"])
        self.assertFalse(result["steps"][0]["ok"])

    def test_confirmation_stops_planning_without_another_action(self):
        agent = self.agent([{"tool": "click"}], "[CONFIRMATION_PENDING] Approve on desktop")
        result = agent.run("click")
        self.assertFalse(result["ok"])
        self.assertEqual(agent._plan.call_count, 1)
        self.assertEqual(agent.registry.run.call_count, 1)

    def test_cancel_during_planning_prevents_action(self):
        agent = self.agent([])
        def plan(objective):
            agent.context["cancel_event"].set()
            return {"tool": "click"}
        agent._plan.side_effect = plan
        self.assertFalse(agent.run("click")["ok"])
        agent.registry.run.assert_not_called()
