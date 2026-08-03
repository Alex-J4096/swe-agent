import unittest
from pathlib import Path
from uuid import UUID

from src.runtime.session import Session
from src.utils.welcome import format_workdir, render_welcome


class SessionTests(unittest.TestCase):
    def test_session_id_is_a_unique_uuid(self):
        first = Session(model="test-model")
        second = Session(model="test-model")

        UUID(first.session_id)
        UUID(second.session_id)
        self.assertNotEqual(first.session_id, second.session_id)


class WelcomeTests(unittest.TestCase):
    def test_home_directory_is_rendered_compactly(self):
        self.assertEqual(format_workdir(Path.home()), "~")

    def test_panel_contains_session_directory_and_model(self):
        session = Session(model="test-model", session_id="session-123")
        workdir = Path("/tmp/project")

        panel = render_welcome(session, workdir)

        self.assertIn("Welcome to SWE Agent!", panel)
        self.assertIn(f"Directory: {format_workdir(workdir)}", panel)
        self.assertIn("Session:   session-123", panel)
        self.assertIn("Model:     test-model", panel)


if __name__ == "__main__":
    unittest.main()
