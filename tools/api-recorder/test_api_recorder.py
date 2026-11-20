"""Tests for the standalone API recorder tools.

Run:  python -m pytest tools/api-recorder -q
The browser tests need a launchable Chromium: Playwright's own, or set
NODE_REC_CHROMIUM=/path/to/chromium. Without one they skip; the rest still run.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("record", HERE / "record.py")
record = importlib.util.module_from_spec(spec)
spec.loader.exec_module(record)

SECRETS = ["SECRET-TOKEN", "eyJhbGci", "SECRETCANARY999", "Lab report", "Maria", "mlee@", "diaz@", "goggles",
           "AAQkAGI2", "Band concert", "from:maria"]

# --------------------------------------------------------------- fake OWA
PAGE = """<!doctype html><html><body><h1>Fake OWA</h1>
<button id="folder" onclick="loadList()">inbox</button>
<button id="open" onclick="openItem()">open email</button>
<script>
const TOKEN = "Bearer SECRET-TOKEN-eyJhbGciOi123";
async function loadList() {
  await fetch("/owa/service.svc?action=FindConversation&app=Mail&n=12", {method: "POST",
    headers: {"Authorization": TOKEN, "action": "FindConversation", "Content-Type": "application/json; charset=utf-8"},
    body: JSON.stringify({__type: "FindConversationJsonRequest:#Exchange", Body: {QueryString: "from:maria"}})});
  fetch("/owa/ev.owa2?ns=PerfMark", {method: "POST", body: "x"});
}
function openItem() {
  const x = new XMLHttpRequest();
  x.open("POST", "/owa/service.svc?action=GetItem&app=Mail&n=55");
  x.setRequestHeader("Authorization", TOKEN);
  x.setRequestHeader("x-owa-urlpostdata", encodeURIComponent(JSON.stringify({__type: "GetItemJsonRequest:#Exchange", Body: {ItemShape: {BaseShape: "IdOnly"}}})));
  x.send();
}
loadList();
</script></body></html>"""
LIST = {"Body": {"ResponseMessages": {"Items": [{"Conversation": [
    {"ConversationTopic": "Lab report due Friday", "UniqueSenders": ["Maria Lee"],
     "From": {"Mailbox": {"EmailAddress": "mlee@thedps.org"}}, "UnreadCount": 1,
     "ConversationId": {"Id": "AAQkAGI2NGVhZTVlLTI3ZjUtNDQ1Ni1hYjE0"}},
    {"ConversationTopic": "Band concert", "UniqueSenders": ["Ms. Diaz"],
     "From": {"Mailbox": {"EmailAddress": "diaz@thedps.org"}}, "UnreadCount": 0,
     "ConversationId": {"Id": "AAQkAGI2NGVhZTVlLTI3ZjUtNDQ1Ni1hYjE1"}}]}]}}}
ITEM = {"Body": {"ResponseMessages": {"Items": [{"Items": [
    {"Subject": "Lab report due Friday", "Body": {"Value": "<p>Bring goggles, Maria</p>", "BodyType": "HTML"}}]}]}}}


class FakeOwa(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Set-Cookie", "X-OWA-CANARY=SECRETCANARY999; Path=/")
        # Strict like Microsoft's apps can be: Trusted Types + no inline styles.
        self.send_header("Content-Security-Policy", "require-trusted-types-for 'script'; style-src 'self'")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, PAGE, "text/html")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if "FindConversation" in self.path:
            return self._send(200, json.dumps(LIST), "application/json; charset=utf-8")
        if "GetItem" in self.path:
            return self._send(200, json.dumps(ITEM), "application/json")
        self._send(204, b"", "text/plain")


@pytest.fixture(scope="module")
def owa():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOwa)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def chromium_path():
    return os.getenv("NODE_REC_CHROMIUM") or None


async def _launch(pw):
    try:
        return await pw.chromium.launch(executable_path=chromium_path(), headless=True, args=["--no-sandbox"])
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no launchable Chromium: {str(exc).splitlines()[0]}")


# --------------------------------------------------------- no browser needed
def test_console_snippet_is_built_from_shared_shape_js():
    built = (HERE / "console-recorder.js").read_text()
    shape = (HERE.parents[1] / "extension" / "recorder" / "shape.js").read_text()
    main = (HERE / "src" / "console-main.js").read_text()
    assert shape in built and main in built, "run: node tools/api-recorder/build.mjs"
    assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML", main), "Trusted Types sites block innerHTML from pasted code"


def test_recorder_groups_marks_and_exports_without_a_browser():
    rec = record.Recorder([], own_origin="http://127.0.0.1:1")
    base = {"names": ["action", "authorization"], "safe": {"action": "FindConversation"}, "cookieNames": ["X-OWA-CANARY"],
            "reqShape": {"Body": {"QueryString": "<text 10>"}}, "respShape": None, "noise": False, "suggested": True,
            "messageList": False, "via": "fetch", "origin": "https://outlook.cloud.microsoft"}
    key = "POST outlook.cloud.microsoft/owa/service.svc · FindConversation"
    rec.add({**base, "key": key, "url": "https://outlook.cloud.microsoft/owa/service.svc?action=FindConversation", "t": 1, "status": 200})
    rec.add({**base, "key": key, "url": "https://outlook.cloud.microsoft/owa/service.svc?action=FindConversation", "t": 2, "status": 200,
             "respShape": {"Items": [{"Subject": "<text 3>", "From": "<email>"}]}, "messageList": True})
    rec.add({**base, "key": "POST x/ev", "url": "https://x/ev", "t": 3, "status": 204, "noise": True, "suggested": True})
    gs = {g["key"]: g for g in rec.groups()}
    g = gs[key]
    assert g["count"] == 2 and g["messageList"] and g["suggested"]
    assert g["responseBody"] == {"Items": [{"Subject": "<text 3>", "From": "<email>"}]}
    assert g["auth"] == "authorization header + cookies"
    assert gs["POST x/ev"]["suggested"] is False, "noise is never suggested"
    assert rec.export()["calls"] == []
    rec.marks[key] = "inbox list"
    exp = rec.export()
    assert exp["site"] == "https://outlook.cloud.microsoft"
    assert [c["label"] for c in exp["calls"]] == ["inbox list"]
    assert record.export_filename(exp).startswith("node-api-capture-outlook.cloud.microsoft-")
    assert rec.wants("https://a.example/x", "") and not rec.wants("http://127.0.0.1:1/api/state", "")
    rec.matches = ["outlook"]
    assert not rec.wants("https://a.example/x", "https://a.example/") and rec.wants("https://a.example/x", "https://outlook.office.com/mail")


def test_ui_server_routes_and_host_check():
    async def go():
        rec = record.Recorder([], own_origin="")
        ui = record.UiServer(rec, "/*shape*/", 0)
        await ui.start()
        loop = asyncio.get_running_loop()

        def get(path, host=None, data=None):
            req = urllib.request.Request(ui.url.rstrip("/") + path, data=data, headers={"Host": host} if host else {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, e.read(), dict(e.headers)

        status, body, _ = await loop.run_in_executor(None, get, "/")
        assert status == 200 and b"API Recorder" in body
        assert (await loop.run_in_executor(None, get, "/shape.js"))[1] == b"/*shape*/"
        status, body, _ = await loop.run_in_executor(None, get, "/api/state")
        assert json.loads(body)["groups"] == []
        status, _, _ = await loop.run_in_executor(None, lambda: get("/api/mark", data=json.dumps({"key": "K", "mark": "x"}).encode()))
        assert status == 200 and rec.marks == {"K": "x"}
        status, _, hdrs = await loop.run_in_executor(None, get, "/api/export")
        assert status == 200 and "attachment" in hdrs.get("Content-Disposition", "")
        status, _, _ = await loop.run_in_executor(None, get, "/api/state", "evil.example:80")
        assert status == 403, "other origins (DNS rebinding) must be refused"
        ui.server.close()

    asyncio.run(go())


# ---------------------------------------------------------- browser tests
def test_console_snippet_records_marks_and_saves_under_strict_csp(owa):
    from playwright.async_api import async_playwright

    snippet = (HERE / "console-recorder.js").read_text()

    async def go():
        async with async_playwright() as pw:
            browser = await _launch(pw)
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(owa + "/owa/")
            await page.wait_for_timeout(500)
            await page.evaluate(snippet)  # what pasting into the console does
            assert await page.evaluate("!!document.getElementById('node-api-recorder-console').shadowRoot.querySelector('.box')")
            # the panel is styled although inline <style> is blocked by CSP
            bg = await page.evaluate("getComputedStyle(document.getElementById('node-api-recorder-console').shadowRoot.querySelector('.box')).position")
            assert bg == "fixed"
            await page.click("#folder")  # re-load the list after pasting
            await page.wait_for_timeout(600)
            shadow = "document.getElementById('node-api-recorder-console').shadowRoot"
            await page.evaluate(f"[...{shadow}.querySelectorAll('button')].find(b => b.textContent.includes('Watch')).click()")
            await page.wait_for_timeout(400)
            await page.click("#open")
            await page.wait_for_timeout(8000)
            msg = await page.evaluate(f"{shadow}.querySelector('.msg').innerText")
            assert "1 call type(s) fired" in msg, msg
            await page.evaluate(f"{shadow}.querySelector('.msg input').value = 'open an email'")
            await page.evaluate(f"[...{shadow}.querySelectorAll('.msg button')].find(b => b.textContent === 'Mark them').click()")
            groups = await page.evaluate("nodeRec.groups()")
            find = next(g for g in groups if "FindConversation" in g["key"])
            assert find["suggested"] and find["messageList"]
            assert any(g["noise"] for g in groups if "ev.owa2" in g["key"])
            await page.evaluate(f"nodeRec.mark({json.dumps(find['key'])}, 'inbox list')")
            data = await page.evaluate("nodeRec.export()")
            labels = sorted(c["label"] for c in data["calls"])
            assert labels == ["inbox list", "open an email"], labels
            get_item = next(c for c in data["calls"] if c["label"] == "open an email")
            assert get_item["requestBody"]["__type"] == "GetItemJsonRequest:#Exchange"
            assert "x-owa-urlpostdata" in get_item["requestHeaderNames"]
            text = json.dumps(data)
            assert [s for s in SECRETS if s in text] == []
            # save() triggers a real download
            async with page.expect_download() as dl:
                await page.evaluate("nodeRec.save()")
            assert (await dl.value).suggested_filename.startswith("node-api-capture-127.0.0.1-")
            # stop() restores the page's own fetch
            await page.evaluate("nodeRec.stop()")
            assert await page.evaluate("window.fetch.toString().includes('[native code]')")
            assert not errors, errors
            await browser.close()

    asyncio.run(go())


def test_python_recorder_end_to_end(owa, tmp_path):
    async def go():
        args = SimpleNamespace(url=None, match=[owa.split("//")[1]], channel=None, browser=chromium_path(), cdp=None,
                               profile=str(tmp_path / "profile"), port=0, headless=True)
        app = record.App(args)
        try:
            await app.start()
        except Exception as exc:  # pragma: no cover - environment dependent
            await app.close()
            pytest.skip(f"no launchable Chromium: {str(exc).splitlines()[0]}")
        try:
            page = await app.context.new_page()
            await page.goto(owa + "/owa/")
            await page.click("#open")
            await page.wait_for_timeout(2500)
            gs = {g["key"].split(" · ")[-1]: g for g in app.rec.groups()}
            assert {"FindConversation", "GetItem"} <= set(gs), list(gs)
            find = gs["FindConversation"]
            # startup call caught, with real header and cookie NAMES
            assert find["suggested"] and find["messageList"]
            assert "authorization" in find["requestHeaderNames"]
            assert find["cookieNames"] == ["X-OWA-CANARY"]
            assert find["requestHeaders"].get("action") == "FindConversation"
            assert find["requestBody"]["Body"]["QueryString"].startswith("<text")
            assert gs["GetItem"]["requestBody"]["__type"] == "GetItemJsonRequest:#Exchange"
            # the marking page works in the browser
            ui = app.rec.shaper.page
            await ui.reload()
            await ui.wait_for_timeout(1800)
            assert await ui.locator(".row").count() >= 2
            await ui.locator(".row", has_text="FindConversation").locator("input[type=checkbox]").click()
            await ui.wait_for_timeout(600)
            label = ui.locator(".row", has_text="FindConversation").locator("input.label")
            await label.fill("inbox list")
            await label.press("Tab")
            await ui.wait_for_timeout(400)
            assert app.rec.marks.get(find["key"]) == "inbox list"
            data = app.rec.export()
            assert [c["label"] for c in data["calls"]] == ["inbox list"]
            assert data["site"] == owa
            text = json.dumps(app.rec.export(include_all=True))
            assert [s for s in SECRETS if s in text] == []
            os.chdir(tmp_path)
            out = app.save_marked()
            assert out and json.loads(out.read_text())["calls"][0]["label"] == "inbox list"
        finally:
            await app.close()

    asyncio.run(go())
