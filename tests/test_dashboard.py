"""The dashboard: state, Strat-document uploads, safety — over real HTTP on a free port."""

import json
import urllib.error
import urllib.request

import pytest
from conftest import DAILY_INSIDE, FakeBrain, failing_source, inst, source_for

from trader import dashboard
from trader.config import settings
from trader.engine import Engine
from trader.hyperliquid import PaperBroker


@pytest.fixture()
def server(journal, wednesday, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "strat_docs_dir", str(tmp_path / "docs"))
    from trader import knowledge

    monkeypatch.setattr(knowledge, "_library", knowledge.Library([knowledge.BUILTIN, tmp_path / "docs"]))
    prices = {"xyz:NVDA": 108.5}
    e = Engine(journal, PaperBroker(journal, price_fn=lambda c: prices[c]), brain=FakeBrain(), instruments=lambda: [inst()],
               chart_source=source_for(DAILY_INSIDE), perp_source=failing_source, clock=lambda: wednesday)
    srv = dashboard.serve(e, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, e, prices, tmp_path / "docs"
    srv.shutdown()


def call(base, path, method="GET", body=None, headers=None):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_the_page_and_its_state(server):
    base, e, prices, _ = server
    code, html = call(base, "/")
    assert code == 200 and b"Strat documents" in html
    code, body = call(base, "/api/state")
    s = json.loads(body)
    assert code == 200 and s["mode"] == "paper" and s["equity"] == 1000 and s["min_week"] == 2
    assert any(d["name"] == "01_THE_STRAT_PLAYBOOK.md" and not d["yours"] for d in s["docs"])


def test_upload_search_and_delete_a_strat_document(server):
    base, e, prices, folder = server
    text = b"My Strat notes: only take a 2-1-2 on the daily when the weekly is a 2U. Volume dries up in the inside bar."
    code, body = call(base, "/api/docs", "POST", text, {"X-Filename": "my%20weekly%20rules.md"})
    r = json.loads(body)
    assert code == 200 and r["saved"] == "my weekly rules.md" and r["pieces"] == 1
    assert (folder / "my weekly rules.md").read_bytes() == text
    code, _ = call(base, "/api/docs", "POST", text, {"X-Filename": "my%20weekly%20rules.md"})
    assert (folder / "my weekly rules (2).md").exists()  # never overwritten
    hits = json.loads(call(base, "/api/docs/search?q=weekly%202U%20inside")[1])["hits"]
    assert hits[0]["file"].startswith("my weekly rules")
    from trader.knowledge import library

    assert "weekly is a 2U" in library().context("2-1-2 daily weekly 2U")  # Trader's prompts now use it
    code, _ = call(base, "/api/docs?name=my%20weekly%20rules.md", "DELETE")
    assert code == 200 and not (folder / "my weekly rules.md").exists()


def test_uploads_are_kept_safe(server):
    base, e, prices, folder = server
    assert call(base, "/api/docs", "POST", b"x", {"X-Filename": "run.sh"})[0] == 400
    code, _ = call(base, "/api/docs", "POST", b"hello there notes", {"X-Filename": "..%2F..%2Fescape.md"})
    assert code == 200 and (folder / "escape.md").exists() and not (folder.parent.parent / "escape.md").exists()
    assert call(base, "/api/docs?name=..%2F..%2Fetc%2Fpasswd", "DELETE")[0] == 400
    assert call(base, "/api/docs?name=01_THE_STRAT_PLAYBOOK.md", "DELETE")[0] == 400  # built-in playbook stays
    # another website in the boss's browser can't post to the dashboard
    assert call(base, "/api/scan", "POST", b"", {"Origin": "https://evil.example"})[0] == 403


def test_scan_is_read_only_and_close_works(server, journal):
    base, e, prices, _ = server
    scan = json.loads(call(base, "/api/scan", "POST", b"")[1])
    assert scan["setups"][0]["inside"] and not journal.all("SELECT * FROM plans")
    e.scan()  # the real loop plans it…
    prices["xyz:NVDA"] = 110.1
    e.watch_plans(e.clock())
    t = journal.open_trades()[0]
    s = json.loads(call(base, "/api/state")[1])
    assert s["positions"][0]["coin"] == "xyz:NVDA" and s["positions"][0]["unrealized"] is not None
    code, body = call(base, "/api/close", "POST", {"id": t["id"]}, {"Content-Type": "application/json"})
    assert code == 200 and journal.trade(t["id"])["exit_reason"] == "manual"
    assert json.loads(call(base, "/api/state")[1])["pnl_series"]


def test_opening_beyond_this_mac_needs_a_key(journal, monkeypatch):
    monkeypatch.setattr(settings, "dash_key", "")
    with pytest.raises(RuntimeError, match="TRADER_DASH_KEY"):
        dashboard.serve(object(), host="0.0.0.0", port=0)


def test_safe_filenames():
    assert dashboard.safe_filename("Strat%20Course%20Notes.pdf") == "Strat Course Notes.pdf"
    with pytest.raises(ValueError):
        dashboard.safe_filename(".env")
    with pytest.raises(ValueError):
        dashboard.safe_filename("evil.py")


def test_requests_from_other_devices_need_the_key(monkeypatch):
    from email.message import Message

    H = dashboard.make_handler(dashboard.Dashboard(object()))
    h = H.__new__(H)
    h.headers = Message()
    h.client_address = ("192.168.1.50", 5000)
    monkeypatch.setattr(settings, "dash_key", "s3cret-key")
    assert not h._authorized()
    h.headers["X-Trader-Key"] = "wrong"
    assert not h._authorized()
    h.headers.replace_header("X-Trader-Key", "s3cret-key")
    assert h._authorized()
    h.client_address = ("127.0.0.1", 5000)
    del h.headers["X-Trader-Key"]
    assert h._authorized()  # this Mac itself
