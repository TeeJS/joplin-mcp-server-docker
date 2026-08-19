import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.joplin.joplin_api import JoplinNotebook, JoplinNote
from src.mcp import joplin_mcp


class FakeAPI:
    def __init__(self, notebooks=None):
        self.notebooks = notebooks or []
        self.created_notebook_calls = []
        self.created_note_calls = []
        self.updated_note_calls = []

    def list_notebooks(self):
        return self.notebooks

    def create_notebook(self, title, parent_id=None):
        self.created_notebook_calls.append({"title": title, "parent_id": parent_id})
        return JoplinNotebook(id="new-notebook", title=title, parent_id=parent_id, children=[])

    def create_note(self, title, body=None, parent_id=None, is_todo=False):
        self.created_note_calls.append(
            {"title": title, "body": body, "parent_id": parent_id, "is_todo": is_todo}
        )
        return JoplinNote(
            id="new-note",
            title=title,
            body=body,
            parent_id=parent_id,
            is_todo=is_todo,
            created_time=datetime(2026, 3, 20, 12, 0, 0),
            updated_time=datetime(2026, 3, 20, 12, 0, 0),
        )

    def update_note(self, note_id, title=None, body=None, parent_id=None, is_todo=None):
        self.updated_note_calls.append(
            {
                "note_id": note_id,
                "title": title,
                "body": body,
                "parent_id": parent_id,
                "is_todo": is_todo,
            }
        )
        return JoplinNote(
            id=note_id,
            title=title or "Existing",
            body=body,
            parent_id=parent_id,
            is_todo=bool(is_todo),
            created_time=datetime(2026, 3, 20, 12, 0, 0),
            updated_time=datetime(2026, 3, 20, 12, 5, 0),
        )


class JoplinMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_api = joplin_mcp.api

    def tearDown(self):
        joplin_mcp.api = self.original_api

    def test_resolve_notebook_id_by_full_path_when_title_is_ambiguous(self):
        joplin_mcp.api = FakeAPI(
            notebooks=[
                JoplinNotebook(
                    id="work",
                    title="Work",
                    children=[JoplinNotebook(id="work-projects", title="Projects", children=[])],
                ),
                JoplinNotebook(
                    id="personal",
                    title="Personal",
                    children=[JoplinNotebook(id="personal-projects", title="Projects", children=[])],
                ),
            ]
        )

        notebook_id = joplin_mcp.resolve_notebook_id(None, "Work/Projects")

        self.assertEqual(notebook_id, "work-projects")

    def test_resolve_notebook_id_raises_for_ambiguous_title(self):
        joplin_mcp.api = FakeAPI(
            notebooks=[
                JoplinNotebook(
                    id="work",
                    title="Work",
                    children=[JoplinNotebook(id="work-projects", title="Projects", children=[])],
                ),
                JoplinNotebook(
                    id="personal",
                    title="Personal",
                    children=[JoplinNotebook(id="personal-projects", title="Projects", children=[])],
                ),
            ]
        )

        with self.assertRaisesRegex(ValueError, "matches multiple notebooks"):
            joplin_mcp.resolve_notebook_id(None, "Projects")

    async def test_create_notebook_resolves_parent_by_name(self):
        fake_api = FakeAPI(
            notebooks=[JoplinNotebook(id="work", title="Work", children=[])]
        )
        joplin_mcp.api = fake_api

        result = await joplin_mcp.create_notebook(
            title="Projects", parent_notebook_name="Work"
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(fake_api.created_notebook_calls[0]["parent_id"], "work")

    async def test_create_note_accepts_notebook_name(self):
        fake_api = FakeAPI(
            notebooks=[
                JoplinNotebook(
                    id="work",
                    title="Work",
                    children=[JoplinNotebook(id="work-projects", title="Projects", children=[])],
                )
            ]
        )
        joplin_mcp.api = fake_api

        result = await joplin_mcp.create_note(
            title="Roadmap", body="Draft", notebook_name="Work/Projects"
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(fake_api.created_note_calls[0]["parent_id"], "work-projects")

    async def test_import_markdown_accepts_notebook_name(self):
        fake_api = FakeAPI(
            notebooks=[JoplinNotebook(id="work", title="Work", children=[])]
        )
        joplin_mcp.api = fake_api

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "note.md"
            file_path.write_text("# Imported Note\n\nBody text", encoding="utf-8")

            result = await joplin_mcp.import_markdown(
                file_path=str(file_path), notebook_name="Work"
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(fake_api.created_note_calls[0]["title"], "Imported Note")
        self.assertEqual(fake_api.created_note_calls[0]["parent_id"], "work")


if __name__ == "__main__":
    unittest.main()
