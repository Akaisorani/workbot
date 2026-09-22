import unittest

from workbot.tasks.routing import (
    configured_node_in_text,
    configured_nodes_in_text,
    has_windows_local_hint,
    is_explicit_remote_action,
    looks_like_mixed_workflow,
)


class TaskRoutingTests(unittest.TestCase):
    def test_explicit_node_action_routes_without_task_classification(self):
        text = "请在linux-server1查看日志并定位失败原因"
        self.assertEqual(configured_node_in_text(text, ["linux-server1", "dev2"]), "linux-server1")
        self.assertTrue(is_explicit_remote_action(text, ["linux-server1", "dev2"]))

    def test_plain_question_does_not_route_as_imperative(self):
        self.assertFalse(is_explicit_remote_action("linux-server1是什么服务器？", ["linux-server1"]))

    def test_detect_multiple_nodes(self):
        self.assertEqual(
            configured_nodes_in_text("分别在linux-server1和dev2检查代码", ["linux-server1", "dev2", "dev3"]),
            ["linux-server1", "dev2"],
        )

    def test_windows_path_is_local_hint(self):
        self.assertTrue(has_windows_local_hint(r"读取D:\\docs\\spec.md，然后让linux-server1检查实现"))

    def test_mixed_workflow_trigger(self):
        text = r"先读取Windows本地D:\\docs\\spec.md，再让linux-server1检查代码并结合结果给我结论"
        self.assertTrue(looks_like_mixed_workflow(text, ["linux-server1"]))
        self.assertFalse(looks_like_mixed_workflow("在linux-server1创建/tmp/test.txt", ["linux-server1"]))


if __name__ == "__main__":
    unittest.main()
