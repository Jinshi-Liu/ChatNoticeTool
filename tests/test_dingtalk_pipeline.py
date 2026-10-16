from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dingtalk_export as de
import todo_tasks as tt


def msg(mid, sender, time, text):
    return {"platform": "dingtalk", "group_id": "G1", "group_name": "测试群",
            "message_id": mid, "sender": sender, "time": time,
            "body_status": "decoded", "text": text}


class ActionableFilterTests(unittest.TestCase):
    def test_imperative_matched(self):
        self.assertTrue(tt.is_actionable("请大家今天提交周报"))

    def test_deadline_matched(self):
        self.assertTrue(tt.is_actionable("记得截止日期是周五前完成"))

    def test_mention_matched(self):
        self.assertTrue(tt.is_actionable("@张三 帮忙看下这个问题"))

    def test_plain_chat_not_actionable(self):
        self.assertFalse(tt.is_actionable("今天天气不错"))


class ClusteringTests(unittest.TestCase):
    def test_same_task_merged(self):
        msgs = [
            msg("1", "A", "2026-10-09T21:52:00", "各位同学收到请再群里回复1"),
            msg("2", "A", "2026-10-09T21:53:00", "@10"),
        ]
        clusters = tt.cluster_tasks(msgs, gap_min=30, min_sim=0.3)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 2)

    def test_different_tasks_split(self):
        msgs = [
            msg("1", "A", "2026-10-09T21:52:00", "请大家今天提交周报"),
            msg("2", "B", "2026-10-09T21:53:00", "明天的会议改到下午三点"),
        ]
        clusters = tt.cluster_tasks(msgs, gap_min=30, min_sim=0.3)
        self.assertEqual(len(clusters), 2)

    def test_time_gap_splits_burst(self):
        msgs = [
            msg("1", "A", "2026-10-09T21:52:00", "请大家今天提交周报"),
            msg("2", "A", "2026-10-09T23:00:00", "请大家今天提交周报"),
        ]
        clusters = tt.cluster_tasks(msgs, gap_min=30, min_sim=0.3)
        self.assertEqual(len(clusters), 2)


class RecordTests(unittest.TestCase):
    def test_record_keeps_all_sources(self):
        msgs = [
            msg("1", "A", "2026-10-09T21:52:00", "请大家今天提交周报"),
            msg("2", "A", "2026-10-09T21:53:00", "收到请回复1"),
        ]
        rec = tt.build_task_record(msgs)
        self.assertEqual(rec["message_count"], 2)
        need = {"platform", "group_id", "group_name", "message_id", "sender", "time"}
        for s in rec["sources"]:
            self.assertTrue(need <= set(s))

    def test_stable_task_id(self):
        msgs = [msg("1", "A", "2026-10-09T21:52:00", "请大家今天提交周报"),
                msg("2", "A", "2026-10-09T21:53:00", "收到请回复1")]
        a = tt.build_task_record(msgs)["task_id"]
        b = tt.build_task_record(list(reversed(msgs)))["task_id"]
        self.assertEqual(a, b)  # 消息顺序无关，ID 稳定


class TodoTests(unittest.TestCase):
    def test_merged_title_flags_count(self):
        msgs = [msg("1", "A", "2026-10-09T21:52:00", "请大家今天提交周报"),
                msg("2", "A", "2026-10-09T21:53:00", "收到请回复1")]
        todo = tt.build_todo(tt.build_task_record(msgs))
        self.assertIn("2 条合并", todo["title"])
        self.assertIn("请大家今天提交周报", todo["note"])


class ExportGuardTests(unittest.TestCase):
    def test_require_confirm(self):
        with self.assertRaises(Exception):
            de.export({"dingtalk": {}}, {"confirmed": False})

    def test_duplicate_group_rejected(self):
        with self.assertRaises(ValueError):
            de.export({"dingtalk": {}}, {"confirmed": True,
                                         "group_ids": ["1", "1"]})


if __name__ == "__main__":
    unittest.main()
