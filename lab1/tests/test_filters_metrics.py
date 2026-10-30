from src.evaluation.metrics import (hit_at_k, keyword_present, keyword_recall, precision_at_k, ranked_docs,
                                    recall_at_k, reciprocal_rank)
from src.pipeline import detect_question_lang, detect_text_lang, is_refusal, sanitize_citations
from src.generation.prompts import REFUSALS, build_messages
from src.retrieval.filters import apply_filters, deduplicate, jaccard, max_per_document
from src.retrieval.vector_store import Hit


def hit(cid, score, text="text " * 40, doc="d1", url="https://utm.md/a/"):
    return Hit(cid, text, score, {"document_id": doc, "url": url})


def test_jaccard_identical_and_different():
    assert jaccard("a b c d e", "a b c d e") == 1.0
    assert jaccard("a b c d e", "x y z w q") == 0.0


def test_deduplicate_keeps_first_of_near_duplicates():
    text = "Sesiunea de examinare începe pe 5 ianuarie și durează trei săptămâni pentru toți studenții."
    hits = [hit("1", 0.9, text, "d1"), hit("2", 0.85, text + " FCIM", "d2"), hit("3", 0.8, "cu totul alt subiect aici", "d3")]
    assert [h.chunk_id for h in deduplicate(hits, 0.8)] == ["1", "3"]


def test_max_per_document():
    hits = [hit("1", 0.9), hit("2", 0.8), hit("3", 0.7), hit("4", 0.6, doc="d2")]
    assert [h.chunk_id for h in max_per_document(hits, 2)] == ["1", "2", "4"]


def test_apply_filters_threshold_and_trace():
    cfg = {"filters": {"score_threshold": 0.8, "min_chunk_chars": 10, "dedup_threshold": None, "max_per_document": None}}
    kept, trace = apply_filters([hit("1", 0.9), hit("2", 0.7)], cfg)
    assert [h.chunk_id for h in kept] == ["1"]
    assert trace == {"retrieved": 2, "after_threshold": 1, "after_length": 1, "after_dedup": 1, "after_per_doc": 1}


def test_ranking_metrics():
    ranked = ["a", "b", "c", "d"]
    relevant = {"c", "x"}
    assert hit_at_k(ranked, relevant, 2) == 0.0
    assert hit_at_k(ranked, relevant, 3) == 1.0
    assert recall_at_k(ranked, relevant, 4) == 0.5
    assert precision_at_k(ranked, relevant, 4) == 0.25
    assert reciprocal_rank(ranked, relevant) == 1 / 3


def test_ranked_docs_collapses_chunks_of_same_document():
    hits = [hit("1", 0.9, doc="a"), hit("2", 0.8, doc="a"), hit("3", 0.7, doc="b")]
    assert ranked_docs(hits) == ["a", "b"]


def test_keyword_recall_ignores_case_and_diacritics():
    assert keyword_recall("Taxa de școlarizare este 18000 lei", ["scolarizare", "18000", "euro"]) == 2 / 3


def test_keyword_alternatives_and_digit_grouping():
    assert keyword_present("It lasts four years", "4|four")
    assert keyword_present("5,140 students", "5140")
    assert not keyword_present("2 years", "4|four")


def test_question_language_and_refusal_detection():
    assert detect_question_lang("Когда начинается сессия?") == "ru"
    assert detect_question_lang("Când începe sesiunea?") == "ro"
    assert detect_question_lang("When does the session start?") == "en"
    assert all(is_refusal(r) for r in REFUSALS.values())
    assert is_refusal("Nu am suficientă informații în sursele disponibile pentru a răspunde. [1]")  # changed ending
    assert not is_refusal("The session starts on 5 January [1].")


def test_sanitize_citations_drops_out_of_range_numbers():
    text, removed = sanitize_citations("Un credit = 30 de ore [85]. Anul are 60 de credite [1].", 3)
    assert text == "Un credit = 30 de ore. Anul are 60 de credite [1]."
    assert removed == 1


def test_detect_text_lang_is_not_fooled_by_quoted_titles():
    assert detect_text_lang("The student must submit the application listed in the Regulament de evaluare [1].") == "en"
    assert detect_text_lang("Studentul trebuie să depună cererea și scrisoarea de motivație [1].") == "ro"
    assert detect_text_lang("Декан FCIM — Dumitru Ciorbă, телефон указан на странице [1].") == "ru"


def test_strict_v2_states_answer_language_and_citation_range():
    hits = [hit("1", 0.9), hit("2", 0.8)]
    messages, used = build_messages("strict_v2", "What is ECTS?", hits, 10_000, "en")
    user = messages[1]["content"]
    assert user.endswith("Answer in English.")
    assert "[1]-[2]" in user
    assert REFUSALS["en"] in messages[0]["content"] and REFUSALS["ro"] not in messages[0]["content"]
    assert len(used) == 2


def test_two_tier_reranker_threshold():
    from src.pipeline import RAGPipeline
    pipe = RAGPipeline.__new__(RAGPipeline)  # only the threshold logic, no models
    pipe.cfg = {"reranker": {"min_score": 0.0, "soft_min_score": -3.0}}
    strong, weak, noise = hit("a", 1.5), hit("b", -0.5), hit("c", -4.0)
    assert pipe._apply_confidence([strong, weak, noise]) == ([strong], "high")
    assert pipe._apply_confidence([weak, noise]) == ([weak], "low")
    assert pipe._apply_confidence([noise]) == ([], "none")
    pipe.cfg = {"reranker": {"min_score": 0.0, "soft_min_score": None}}
    assert pipe._apply_confidence([weak]) == ([], "none")


def test_expand_with_neighbors_merges_adjacent_chunks():
    from src.retrieval.expansion import expand_with_neighbors

    class FakeStore:
        chunks = {f"d1#{i}": (f"Doc | Sec\npart {i}", {}) for i in range(5)}
        def get(self, ids):
            return {i: self.chunks[i] for i in ids if i in self.chunks}

    def h(idx, score):
        return Hit(f"d1#{idx}", f"Doc | Sec\npart {idx}", score, {"document_id": "d1", "chunk_index": idx, "title": "Doc"})

    passages = expand_with_neighbors([h(1, 0.3), h(2, -0.7)], FakeStore(), 1)
    assert len(passages) == 1                      # chunks 0..3 form one contiguous passage
    assert passages[0].text == "Doc | Sec\npart 0\npart 1\npart 2\npart 3"
    assert passages[0].score == 0.3
    assert expand_with_neighbors([h(1, 0.3)], FakeStore(), 0)[0].chunk_id == "d1#1"   # window 0 = unchanged
