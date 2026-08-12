"""Tests for the note link graph."""

import unittest
from datetime import datetime

from src.joplin.joplin_api import JoplinNote, JoplinNotebook, OrderDirection, PaginatedResponse
from src.joplin import joplin_links
from src.mcp import joplin_mcp

# A resource (attachment) id looks exactly like a note id, so the graph has to
# reject it by lookup rather than by shape.
RESOURCE_ID = "ff" * 16


def note_id(n: int) -> str:
    """Deterministic 32-hex id for note n."""
    return f"{n:032x}"


class FakeAPI:
    """Serves a fixed set of notes through the slice of JoplinAPI the graph uses."""

    def __init__(self, notes, notebooks=None):
        self.notes = notes
        self.notebooks = notebooks or []
        self.body_fetches = 0

    def list_notebooks(self):
        return self.notebooks

    def get_notes(self, page=1, limit=100, fields=None, order_by="updated_time",
                  order_dir=OrderDirection.DESC):
        fields = fields or []
        # JoplinNote.from_api_response rejects any row without id and title, so
        # a projection that omits them fails against the real server too.
        if "id" not in fields or "title" not in fields:
            raise ValueError(f"Missing essential fields (id/title) in API response: {fields}")
        if "body" in fields:
            self.body_fetches += 1

        ordered = self.notes
        if order_by == "updated_time":
            ordered = sorted(
                self.notes,
                key=lambda n: n.updated_time or datetime.min,
                reverse=order_dir is OrderDirection.DESC,
            )

        start = (page - 1) * limit
        window = ordered[start:start + limit]
        return PaginatedResponse(items=window, has_more=(start + limit) < len(ordered))


def make_note(n, title, body="", parent_id="nb", updated=None):
    return JoplinNote(
        id=note_id(n),
        title=title,
        body=body,
        parent_id=parent_id,
        updated_time=updated or datetime(2026, 1, 1, 12, 0, 0),
    )


def link_to(n, label="link"):
    return f"[{label}](:/{note_id(n)})"


class LinkGraphTests(unittest.TestCase):
    def setUp(self):
        joplin_links.reset_cache()
        # 1 -> 2, 1 -> 3, 2 -> 1 (reciprocal), 3 -> 4, plus noise on 1.
        self.notes = [
            make_note(1, "Alpha", f"See {link_to(2)} and {link_to(3)}. "
                                  f"![img](:/{RESOURCE_ID}) self {link_to(1)}"),
            make_note(2, "Beta", f"Back to {link_to(1)}"),
            make_note(3, "Gamma", f"Onward to {link_to(4)}"),
            make_note(4, "Delta", "No links here"),
            make_note(5, "Epsilon", "Unconnected"),
        ]
        self.notebooks = [
            JoplinNotebook(id="nb", title="Areas", children=[]),
        ]
        self.api = FakeAPI(self.notes, self.notebooks)

    def build(self):
        return joplin_links.build_link_graph(self.api)

    def test_parses_links_and_ignores_resources_and_self_links(self):
        graph = self.build()

        self.assertEqual(graph.outgoing[note_id(1)], {note_id(2), note_id(3)})
        self.assertNotIn(RESOURCE_ID, graph.outgoing[note_id(1)])
        self.assertNotIn(note_id(1), graph.outgoing[note_id(1)])

    def test_builds_reverse_map(self):
        graph = self.build()

        self.assertEqual(graph.incoming[note_id(3)], {note_id(1)})
        self.assertEqual(graph.incoming[note_id(1)], {note_id(2)})
        self.assertNotIn(note_id(5), graph.incoming)

    def test_resolves_notebook_paths(self):
        graph = self.build()

        self.assertEqual(graph.notebooks[note_id(1)], "Areas")

    def test_outbound_only(self):
        result = joplin_links.find_neighbours(self.build(), note_id(1), direction="out")

        self.assertEqual({item["title"] for item in result["links"]}, {"Beta", "Gamma"})

    def test_inbound_only_finds_backlinks(self):
        result = joplin_links.find_neighbours(self.build(), note_id(3), direction="in")

        self.assertEqual([item["title"] for item in result["links"]], ["Alpha"])
        self.assertEqual(result["links"][0]["relation"], "inbound")

    def test_reciprocal_link_reported_as_both(self):
        result = joplin_links.find_neighbours(self.build(), note_id(1), direction="both")

        beta = next(item for item in result["links"] if item["title"] == "Beta")
        self.assertEqual(beta["relation"], "both")

    def test_depth_two_reports_the_intermediate_note(self):
        result = joplin_links.find_neighbours(self.build(), note_id(1), direction="out", depth=2)

        delta = next(item for item in result["links"] if item["title"] == "Delta")
        self.assertEqual(delta["depth"], 2)
        self.assertEqual(delta["via"], "Gamma")
        # First-hop results need no explanation.
        beta = next(item for item in result["links"] if item["title"] == "Beta")
        self.assertIsNone(beta["via"])

    def test_depth_one_does_not_reach_two_hops(self):
        result = joplin_links.find_neighbours(self.build(), note_id(1), direction="out", depth=1)

        self.assertNotIn("Delta", {item["title"] for item in result["links"]})

    def test_unconnected_note_returns_empty(self):
        result = joplin_links.find_neighbours(self.build(), note_id(5))

        self.assertEqual(result["links"], [])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["truncated"])

    def test_limit_truncates_and_flags(self):
        result = joplin_links.find_neighbours(self.build(), note_id(1), direction="out", limit=1)

        self.assertEqual(len(result["links"]), 1)
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["truncated"])

    def test_unknown_note_raises(self):
        with self.assertRaises(KeyError):
            joplin_links.find_neighbours(self.build(), note_id(99))

    def test_bad_direction_raises(self):
        with self.assertRaises(ValueError):
            joplin_links.find_neighbours(self.build(), note_id(1), direction="sideways")

    def test_pagination_covers_every_note(self):
        joplin_links.PAGE_LIMIT, original = 2, joplin_links.PAGE_LIMIT
        try:
            graph = joplin_links.build_link_graph(self.api)
        finally:
            joplin_links.PAGE_LIMIT = original

        self.assertEqual(graph.note_count, 5)
        self.assertEqual(graph.link_count, 4)


class LinkGraphCacheTests(unittest.TestCase):
    def setUp(self):
        joplin_links.reset_cache()
        self.notes = [
            make_note(1, "Alpha", link_to(2)),
            make_note(2, "Beta", ""),
        ]
        self.api = FakeAPI(self.notes)

    def test_second_call_reuses_the_cached_graph(self):
        joplin_links.get_link_graph(self.api)
        joplin_links.get_link_graph(self.api)

        self.assertEqual(self.api.body_fetches, 1)

    def test_refresh_forces_a_rebuild(self):
        joplin_links.get_link_graph(self.api)
        joplin_links.get_link_graph(self.api, refresh=True)

        self.assertEqual(self.api.body_fetches, 2)

    def test_edited_note_invalidates_the_cache(self):
        joplin_links.get_link_graph(self.api)

        self.notes[1].updated_time = datetime(2026, 6, 1, 9, 0, 0)
        self.notes[1].body = link_to(1)
        graph = joplin_links.get_link_graph(self.api)

        self.assertEqual(self.api.body_fetches, 2)
        self.assertEqual(graph.incoming[note_id(1)], {note_id(2)})


class FindLinkedNotesToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        joplin_links.reset_cache()
        self.notes = [
            make_note(1, "Alpha", link_to(2)),
            make_note(2, "Beta", ""),
        ]
        joplin_mcp.api = FakeAPI(self.notes)

    async def test_returns_links_without_bodies(self):
        result = await joplin_mcp.find_linked_notes(note_id(1))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["links"][0]["title"], "Beta")
        # Bodies must never ride along; the caller fetches what it wants.
        self.assertNotIn("body", result["links"][0])

    async def test_backlink_direction(self):
        result = await joplin_mcp.find_linked_notes(note_id(2), direction="in")

        self.assertEqual([item["title"] for item in result["links"]], ["Alpha"])

    async def test_unknown_note_reports_an_error(self):
        result = await joplin_mcp.find_linked_notes(note_id(99))

        self.assertIn("not found", result["error"])

    async def test_bad_direction_reports_an_error(self):
        result = await joplin_mcp.find_linked_notes(note_id(1), direction="sideways")

        self.assertIn("direction must be", result["error"])


if __name__ == "__main__":
    unittest.main()
