"""Grabber logic without network: a fake WordPress source drives the incremental/dedup behaviour."""
from src.grabber import run as grab_run
from src.grabber.sources import FetchedDoc, ListingItem, PdfSource, detect_lang


class FakeWP:
    """Stands in for WordPressSource; `pages` maps id -> (modified, html)."""
    pages: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def list(self, content_type, max_items=None):
        return [ListingItem(i, f"https://utm.md/p{i}/", f"P{i}", m) for i, (m, _) in self.pages.items()]

    def fetch(self, content_type, ids):
        for i in ids:
            m, html = self.pages[i]
            yield FetchedDoc(i, f"https://utm.md/p{i}/", f"P{i}", m, "ro", html, "html")


def make_grabber(tmp_path, monkeypatch):
    monkeypatch.setattr(grab_run, "WordPressSource", FakeWP)
    monkeypatch.setattr(grab_run, "resolve", lambda p: tmp_path / p)
    cfg = {
        "paths": {"raw_dir": "raw", "data_dir": "data"},
        "grabber": {"user_agent": "t", "timeout_s": 1, "max_retries": 1, "delay_s": 0, "per_page": 100,
                    "sites": [{"name": "utm", "base_url": "https://utm.md", "content_types": {"pages": {"max_items": None}}}],
                    "pdf": {"enabled": False}},
    }
    return grab_run.Grabber(cfg)


def test_incremental_run_dedup_and_deletion(tmp_path, monkeypatch):
    FakeWP.pages = {1: ("2026-01-01", "<p>Admitere 2026</p>"), 2: ("2026-01-01", "<p>Burse</p>"),
                    3: ("2026-01-01", "<p>Admitere   2026</p>")}  # same text as 1
    first = make_grabber(tmp_path, monkeypatch).run()
    assert (first["new"], first["duplicates"]) == (2, 1)

    second = make_grabber(tmp_path, monkeypatch).run()
    assert second["new"] == 0 and second["unchanged"] == 3   # nothing downloaded again

    FakeWP.pages[2] = ("2026-02-01", "<p>Burse actualizate</p>")
    FakeWP.pages.pop(1)
    third = make_grabber(tmp_path, monkeypatch).run()
    assert third["updated"] == 1 and third["deleted"] == 1


def test_pdf_relevance_filter():
    src = PdfSource(client=None, base_url="https://utm.md", include=["regulament", "ghid"], exclude=["orar"],
                    max_files=10, max_size_mb=1)
    assert src.is_relevant("https://utm.md/wp-content/uploads/regulament-burse.pdf", "")
    assert not src.is_relevant("https://utm.md/wp-content/uploads/orar-regulament.pdf", "")
    assert not src.is_relevant("https://utm.md/wp-content/uploads/poza.pdf", "Poza")


def test_detect_lang():
    assert detect_lang("https://utm.md/en/admission/") == "en"
    assert detect_lang("https://utm.md/ru/abiturientam/") == "ru"
    assert detect_lang("https://utm.md/admitere/") == "ro"
