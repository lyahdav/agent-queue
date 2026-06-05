import unittest

from agentq.cli import build_parser


class CliTests(unittest.TestCase):
    def test_attach_requires_project_or_run(self):
        parser = build_parser()
        args = parser.parse_args(["attach", "--run", "abc"])
        self.assertEqual(args.run, "abc")

    def test_worker_project_argument(self):
        parser = build_parser()
        args = parser.parse_args(["worker", "--project", "demo"])
        self.assertEqual(args.project, "demo")
        self.assertFalse(args.forever)


if __name__ == "__main__":
    unittest.main()
