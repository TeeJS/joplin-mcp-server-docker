"""Tests for chunking, ranking and rank fusion.

These avoid loading the real model: a fake embedder gives deterministic vectors,
so ranking behaviour is asserted rather than approximated.
"""

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from src.joplin.joplin_api import JoplinNote, PaginatedResponse
from src.joplin import joplin_embeddings as emb


class FakeEmbedder:
    """Maps text to a vector by counting marker words, then normalises."""

    MARKERS = ("alpha", "beta", "gamma", "delta")

    def __init__(self):
        self.calls = 0

    @property
    def dims(self):
        return len(self.MARKERS)

    def encode(self, texts, batch_size=16):
        self.calls += 1
        rows = []
        for text in texts:
            lowered = text.lower()
            vector = np.array(
                [float(lowered.count(m)) for m in self.MARKERS], dtype=np.float32
            )
            norm = np.linalg.norm(vector)
            rows.append(vector / norm if norm else vector)
        return np.array(rows, dtype=np.float32) if rows else np.zeros((0, self.dims), np.float32)


class FakeAPI:
    def __init__(self, notes):
        self.notes = notes

    def get_notes(self, page=1, limit=100, fields=None, **kwargs):
        start = (page - 1) * limit
        window = self.notes[start:start + limit]
        return PaginatedResponse(items=window, has_more=(start + limit) < len(self.notes))


def make_note(note_id, title, body, updated=None, parent_id="nb"):
    return JoplinNote(
        id=note_id, title=title, body=body, parent_id=parent_id,
        updated_time=updated or datetime(2026, 1, 1, 12, 0, 0),
    )


class ChunkingTests(unittest.TestCase):
    def test_title_is_prefixed_to_every_chunk(self):
        body = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(4))
        chunks = emb.split_chunks("My Title", body)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("My Title"))

    def test_titleless_note_still_chunks(self):
        chunks = emb.split_chunks("", "Some body text")

        self.assertEqual(chunks, ["Some body text"])

    def test_empty_body_falls_back_to_the_title(self):
        self.assertEqual(emb.split_chunks("Only Title", ""), ["Only Title"])

    def test_completely_empty_note_yields_nothing(self):
        self.assertEqual(emb.split_chunks("", ""), [])

    def test_long_paragraph_is_split(self):
        chunks = emb.split_chunks("T", "y" * 3000)

        self.assertGreater(len(chunks), 3)

    def test_short_note_is_one_chunk(self):
        self.assertEqual(len(emb.split_chunks("T", "brief body")), 1)


class IndexTests(unittest.TestCase):
    def setUp(self):
        emb.reset_runtime()
        self.embedder = FakeEmbedder()
        self.notes = [
            make_note("n1", "Alpha note", "alpha alpha alpha"),
            make_note("n2", "Beta note", "beta beta beta"),
            make_note("n3", "Mixed note", "alpha beta"),
        ]
        self.api = FakeAPI(self.notes)

    def test_index_covers_every_note(self):
        index = emb.build_index(self.api, self.embedder)

        self.assertEqual(index.note_count, 3)
        self.assertEqual(index.chunk_count, 3)
        self.assertEqual(index.matrix.shape, (3, 4))

    def test_search_ranks_the_matching_note_first(self):
        index = emb.build_index(self.api, self.embedder)
        query = self.embedder.encode(["alpha"])[0]

        hits = emb.semantic_search(index, query, limit=3)

        self.assertEqual(hits[0]["id"], "n1")
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_search_can_exclude_a_note(self):
        index = emb.build_index(self.api, self.embedder)
        query = self.embedder.encode(["alpha"])[0]

        hits = emb.semantic_search(index, query, limit=3, exclude="n1")

        self.assertNotIn("n1", [h["id"] for h in hits])

    def test_note_scores_as_its_best_chunk_not_its_average(self):
        # One sharply relevant paragraph buried in noise must beat a note that is
        # half-relevant throughout. Paragraphs are sized to land in their own
        # chunks, so the relevant one is not diluted by its neighbours.
        buried = make_note("n4", "Buried", "\n\n".join(
            ["gamma " * 70, "alpha " * 70, "gamma " * 70]
        ))
        index = emb.build_index(FakeAPI([buried, self.notes[2]]), self.embedder)
        query = self.embedder.encode(["alpha"])[0]

        hits = emb.semantic_search(index, query, limit=2)
        self.assertEqual(hits[0]["id"], "n4")
        # Averaging over the note's chunks would have put it well below n3's 0.707.
        self.assertGreater(hits[0]["score"], 0.9)

    def test_similar_notes_excludes_itself(self):
        index = emb.build_index(self.api, self.embedder)

        hits = emb.similar_notes(index, "n1", limit=5)

        self.assertNotIn("n1", [h["id"] for h in hits])

    def test_similar_notes_rejects_unknown_note(self):
        index = emb.build_index(self.api, self.embedder)

        with self.assertRaises(KeyError):
            emb.similar_notes(index, "nope", limit=5)

    def test_unchanged_notes_reuse_their_vectors(self):
        first = emb.build_index(self.api, self.embedder)
        calls_after_first = self.embedder.calls

        second = emb.build_index(self.api, self.embedder, previous=first)

        self.assertEqual(self.embedder.calls, calls_after_first)
        np.testing.assert_allclose(first.matrix, second.matrix)

    def test_edited_note_is_re_embedded(self):
        first = emb.build_index(self.api, self.embedder)
        self.notes[1].body = "gamma gamma"
        self.notes[1].updated_time = datetime(2026, 8, 1, 9, 0, 0)

        second = emb.build_index(self.api, self.embedder, previous=first)

        beta = second.note_ids.index("n2")
        chunk = int(np.flatnonzero(second.chunk_note == beta)[0])
        # gamma (marker index 2) must now dominate. Not exactly 1.0, because the
        # chunk carries the note's own title, which still says "Beta".
        self.assertEqual(int(np.argmax(second.matrix[chunk])), 2)
        self.assertGreater(float(second.matrix[chunk][2]), 0.8)

    def test_round_trips_through_disk(self):
        index = emb.build_index(self.api, self.embedder)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.npz"
            original_meta = emb.META_PATH
            emb.META_PATH = path.with_suffix(".meta.json")
            try:
                emb.save_index(index, path)
                loaded = emb.load_index(path)
            finally:
                emb.META_PATH = original_meta

        self.assertEqual(loaded.note_ids, index.note_ids)
        self.assertEqual(loaded.note_parents, index.note_parents)
        np.testing.assert_allclose(loaded.matrix, index.matrix)

    def test_missing_index_loads_as_none(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(emb.load_index(Path(tmp) / "absent.npz"))


class FusionTests(unittest.TestCase):
    def semantic(self, *ids):
        return [
            {"id": i, "title": i, "parent_id": "", "updated": 0, "score": 0.5, "chunk": "c"}
            for i in ids
        ]

    def test_zero_weight_returns_semantic_untouched(self):
        fused = emb.fuse(self.semantic("a", "b"), ["c"], keyword_weight=0.0, limit=5)

        self.assertEqual([h["id"] for h in fused], ["a", "b"])

    def test_no_keyword_hits_returns_semantic_untouched(self):
        fused = emb.fuse(self.semantic("a", "b"), [], keyword_weight=0.5, limit=5)

        self.assertEqual([h["id"] for h in fused], ["a", "b"])

    def test_keyword_only_hit_still_surfaces(self):
        fused = emb.fuse(self.semantic("a"), ["zz"], keyword_weight=0.5, limit=5)

        entry = next(h for h in fused if h["id"] == "zz")
        self.assertEqual(entry["matched"], "keyword")

    def test_agreement_between_rankings_wins(self):
        # 'b' places second semantically but is the only keyword hit, so the two
        # rankings agreeing on it should lift it above the semantic leader.
        # (Exactly reversed rankings at weight 0.5 tie, which is correct RRF and
        # so makes a poor assertion.)
        fused = emb.fuse(self.semantic("a", "b", "c"), ["b"], keyword_weight=0.5, limit=5)

        self.assertEqual(fused[0]["id"], "b")
        self.assertEqual(fused[0]["matched"], "both")

    def test_heavy_keyword_weight_promotes_the_exact_match(self):
        # The identifier case: semantically invisible, keyword rank 1.
        fused = emb.fuse(self.semantic("a", "b", "c"), ["APPS-1"], keyword_weight=0.9, limit=5)

        self.assertEqual(fused[0]["id"], "APPS-1")

    def test_limit_is_respected(self):
        fused = emb.fuse(self.semantic("a", "b", "c"), ["d", "e"], keyword_weight=0.5, limit=2)

        self.assertEqual(len(fused), 2)


if __name__ == "__main__":
    unittest.main()
