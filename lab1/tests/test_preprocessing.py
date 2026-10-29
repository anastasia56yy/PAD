from src.preprocessing.chunking import chunk_document, fixed_chunks, paragraph_chunks, split_sections
from src.preprocessing.cleaning import clean_document, html_to_text, normalize_text, pdf_text_cleanup


def test_html_to_text_keeps_headings_and_lists():
    html = "<h2>Admitere</h2><p>Text despre admitere.</p><ul><li>Act 1</li><li>Act 2</li></ul><script>x()</script>"
    text = html_to_text(html, ["script"])
    assert "## Admitere" in text
    assert "- Act 1" in text and "- Act 2" in text
    assert "x()" not in text


def test_normalize_fixes_cedilla_diacritics_and_spaces():
    assert normalize_text("Facultatea  de  ştiinţe exacte") == "Facultatea de științe exacte"


def test_pdf_cleanup_joins_hyphenated_words_and_marks_articles():
    text = pdf_text_cleanup("Prezentul regula-\nment stabileste\nreguli.\nArt. 5. Durata studiilor\n12")
    assert "regulament" in text
    assert "## Art. 5. Durata studiilor" in text
    assert "\n12" not in text


def test_clean_document_drops_share_boilerplate():
    text = clean_document("<p>Informație utilă despre burse.</p><p>Distribuie pe Facebook</p>", "html")
    assert "Distribuie" not in text


def test_fixed_chunks_respect_size_and_overlap():
    text = " ".join(f"cuvant{i}" for i in range(400))
    plain = fixed_chunks(text, 200)
    overlapped = fixed_chunks(text, 200, 50)
    assert all(len(c) <= 200 for c in plain)
    assert len(overlapped) > len(plain)
    # consecutive overlapped chunks share text
    assert overlapped[0][-30:].split()[-1] in overlapped[1]


def test_paragraph_chunks_do_not_split_short_paragraphs():
    text = "Primul paragraf.\n\nAl doilea paragraf.\n\nAl treilea."
    assert paragraph_chunks(text, 1000) == ["Primul paragraf.\nAl doilea paragraf.\nAl treilea."]


def test_split_sections_builds_heading_path():
    sections = split_sections("# Studii\nintro\n## Licență\ndespre licență\n## Master\ndespre master")
    assert sections == [("Studii", "intro"), ("Studii > Licență", "despre licență"), ("Studii > Master", "despre master")]


def test_heading_chunks_are_prefixed_with_title_and_section():
    chunks = chunk_document("## Burse\n" + "Bursa de merit se acordă studenților. " * 5, "Regulament burse",
                            "heading", 800, 0, min_chunk_chars=10)
    assert chunks[0].text.startswith("Regulament burse | Burse")
    assert chunks[0].section == "Burse"


def test_min_chunk_chars_filters_tiny_chunks():
    chunks = chunk_document("## A\nscurt\n## B\n" + "text lung " * 30, "T", "heading", 800, 0, min_chunk_chars=60)
    assert [c.section for c in chunks] == ["B"]
