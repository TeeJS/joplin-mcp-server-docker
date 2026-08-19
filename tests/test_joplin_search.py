"""Tests for search result shaping."""

import unittest
from datetime import datetime

from src.joplin.joplin_api import JoplinNote, JoplinNotebook, PaginatedResponse
from src.joplin.joplin_utils import build_snippet
from src.joplin import joplin_embeddings as emb
from src.joplin import joplin_links
from src.mcp import joplin_mcp


class SnippetTests(unittest.TestCase):
    def test_short_body_returned_whole(self):
        self.assertEqual(build_snippet("Just a line", "line"), "Just a line")

    def test_empty_body_returns_empty(self):
        self.assertEqual(build_snippet(None, "x"), "")
        self.assertEqual(build_snippet("", "x"), "")

    def test_collapses_whitespace(self):
        self.assertEqual(build_snippet("a\n\n  b\tc", "a"), "a b c")

    def test_centres_on_the_query_term(self):
        # A dated note: the interesting part is nowhere near the start.
        body = "12 May 2026 " + ("filler " * 100) + "PICKLE incident " + ("tail " * 100)
        snippet = build_snippet(body, "pickle", length=90)

        self.assertIn("PICKLE", snippet)
        self.assertNotIn("12 May 2026", snippet)

    def test_falls_back_to_the_start_when_term_is_absent(self):
        body = "Alpha " + ("filler " * 100)
        snippet = build_snippet(body, "nowhere", length=60)

        self.assertTrue(snippet.startswith("Alpha"))
        self.assertTrue(snippet.endswith("…"))

    def test_respects_the_length_budget(self):
        body = "word " * 500
        # Allow for the two ellipsis characters.
        self.assertLessEqual(len(build_snippet(body, "word", length=100)), 102)

    def test_ignores_query_punctuation(self):
        body = ("pad " * 60) + "the TARGET value" + (" pad" * 60)
        self.assertIn("TARGET", build_snippet(body, '"target"', length=80))


class FakeSearchAPI:
    def __init__(self, notes, notebooks=None, has_more=False):
        self.notes = notes
        self.notebooks = notebooks or []
        self.has_more = has_more
        self.search_calls = []

    def list_notebooks(self):
        return self.notebooks

    def search_notes(self, query, limit=100, fields=None):
        self.search_calls.append({"query": query, "limit": limit, "fields": fields})
        return PaginatedResponse(items=self.notes[:limit], has_more=self.has_more)


class SearchNotesToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        joplin_links.reset_cache()
        # Pin "no index" rather than letting get_index() read the real one from
        # the user's cache, which would make these depend on the dev machine.
        emb.set_index(None)
        self.notes = [
            JoplinNote(
                id="a" * 32,
                title="Deploy runbook",
                body="Steps to deploy. " + ("detail " * 200),
                parent_id="ops",
                updated_time=datetime(2026, 5, 1, 9, 0, 0),
            ),
            JoplinNote(
                id="b" * 32,
                title="Empty note",
                body="",
                parent_id="ops",
            ),
        ]
        self.api = FakeSearchAPI(
            self.notes,
            notebooks=[JoplinNotebook(
                id="root", title="Areas",
                children=[JoplinNotebook(id="ops", title="Ops", children=[])],
            )],
        )
        joplin_mcp.api = self.api

    async def test_requests_a_body_from_the_search_endpoint(self):
        await joplin_mcp.search_notes("deploy")

        self.assertIn("body", self.api.search_calls[0]["fields"])

    async def test_default_limit_is_modest(self):
        await joplin_mcp.search_notes("deploy")

        self.assertEqual(self.api.search_calls[0]["limit"], 20)

    async def test_returns_a_snippet_not_the_whole_body(self):
        result = await joplin_mcp.search_notes("deploy")
        entry = result["notes"][0]

        self.assertIn("deploy", entry["snippet"].lower())
        self.assertLess(len(entry["snippet"]), len(self.notes[0].body))
        self.assertNotIn("body", entry)

    async def test_resolves_the_notebook_path(self):
        result = await joplin_mcp.search_notes("deploy")

        self.assertEqual(result["notes"][0]["notebook"], "Areas/Ops")

    async def test_omits_empty_fields_entirely(self):
        result = await joplin_mcp.search_notes("deploy")
        empty = next(n for n in result["notes"] if n["title"] == "Empty note")

        # No snippet, no timestamp, no is_todo - and no nulls standing in.
        self.assertEqual(set(empty), {"id", "title", "notebook"})

    async def test_snippet_chars_zero_omits_excerpts(self):
        result = await joplin_mcp.search_notes("deploy", snippet_chars=0)

        self.assertNotIn("snippet", result["notes"][0])

    async def test_hybrid_falls_back_to_keyword_without_an_index(self):
        result = await joplin_mcp.search_notes("deploy")

        self.assertEqual(result["mode"], "keyword")
        self.assertIn("build_semantic_index", result["notice"])

    async def test_rejects_an_unknown_mode(self):
        result = await joplin_mcp.search_notes("deploy", mode="telepathy")

        self.assertIn("mode must be", result["error"])

    async def test_reports_has_more(self):
        self.api.has_more = True
        result = await joplin_mcp.search_notes("deploy", limit=1)

        self.assertTrue(result["has_more"])
        self.assertEqual(result["total"], 1)

    async def test_payload_is_far_smaller_than_the_old_shape(self):
        import json

        result = await joplin_mcp.search_notes("deploy")
        old_shape = [
            {
                "id": n.id, "title": n.title, "body": n.body,
                "created_time": None, "updated_time": None, "is_todo": False,
            }
            for n in self.notes
        ]

        self.assertLess(len(json.dumps(result["notes"])), len(json.dumps(old_shape)))


if __name__ == "__main__":
    unittest.main()
