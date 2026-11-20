# API Recorder (standalone)

These tools record the API calls a website makes while you use it, let you tick
the ones that matter, and save those to a JSON file. Use them instead of
digging through DevTools. The output is what's needed to write a connector,
e.g. Outlook's inbox list or Teams chats.

The **file holds structure only**:
- method, and the URL with ids masked;
- request header *names*, with values only for protocol headers like `action`
  or `content-type`;
- cookie *names*;
- how the call logs in (bearer token, OWA canary, Skype token, cookies);
- the JSON shape of bodies, where text becomes `<text 21>`, emails `<email>`,
  and ids and tokens are masked.

No message content, tokens or cookie values are saved, so the file is safe to
share. Nothing is uploaded anywhere.

There are three versions. All use the same redaction code
(`extension/recorder/shape.js`) and produce the same file format.

| | Install | Catches calls made at page load | Sees cookie names |
|---|---|---|---|
| **Console snippet** (`console-recorder.js`) | nothing | no: only calls made after you paste it | only non-HttpOnly ones |
| **Python script** (`record.py`) | `pip install playwright` | yes | yes |
| Extension (popup → Record this tab) | the Node extension | yes (reload once) | yes |

## Console snippet: quickest

1. Open the site (e.g. Outlook) and press **F12** → **Console**.
2. Paste the whole of [`console-recorder.js`](console-recorder.js) and press
   Enter. Chrome/Edge may ask you to type `allow pasting` first.
3. A panel appears bottom-right. Calls made before you pasted aren't caught,
   so **switch folder and back** to re-load the inbox list.
4. Click **▶ Watch my next action**, then do one thing within 6 s (open an
   email, open a chat). The calls that fired are highlighted. Name the action,
   then click **Mark them**. You can also tick rows yourself.
5. **Save marked** downloads the file.

Console helpers: `nodeRec.save()`, `nodeRec.save(true)` (every call),
`nodeRec.export()`, `nodeRec.stop()` (restores the page's own `fetch`/XHR),
`nodeRec.show()`.

Reloading the page removes the snippet.

## Python script: catches everything

```bash
pip install playwright
python -m playwright install chromium        # skip if you use --channel msedge / chrome

python tools/api-recorder/record.py --url https://outlook.cloud.microsoft/mail/
```

- A browser window opens with **its own profile**
  (`~/.node-api-recorder/profile`). Log in the first time; later runs stay
  logged in.
- A second tab, **http://127.0.0.1:8765**, is the marking page. It has the same
  **Watch my next action / tick / Save** flow as the snippet. After clicking
  Watch you have 8 s to switch to the site tab and do the thing.
- **Ctrl+C** in the terminal stops it. Anything you marked is also written to
  `./node-api-capture-<site>-<time>.json`.

Options:

| Flag | |
|---|---|
| `--channel msedge` / `chrome` | use the installed Edge/Chrome (no Chromium download) |
| `--browser PATH` | launch another Chromium browser, e.g. Brave's executable |
| `--match outlook,office` | only record URLs/pages containing these words |
| `--cdp http://127.0.0.1:9222` | attach to a browser you started with `--remote-debugging-port=9222` (Chrome 136+ only allows this with a separate `--user-data-dir`) |
| `--profile DIR` | a different profile folder |
| `--port N` | marking-page port (default 8765) |

The script only serves the marking page on `127.0.0.1` and rejects any other
`Host`.

## What to record

- **Outlook:** inbox list (switch folder and back), open an email, scroll
  down the list, open another folder.
- **Teams:** open a chat, scroll up in it, switch chats, receive a message (the
  websocket row).
- Anything else: whichever action shows the data you want in Node.

## For developers

- `console-recorder.js` is **generated**. Edit `src/console-main.js` or
  `extension/recorder/shape.js`, then run `node tools/api-recorder/build.mjs`.
  CI runs `--check`.
- Tests: `python -m pytest tools/api-recorder -q`. The browser tests need a
  Chromium; set `NODE_REC_CHROMIUM=/path` if Playwright's isn't installed.
  They run the snippet on a fake OWA page that enforces Trusted Types and
  blocks inline styles, which is why the snippet never uses `innerHTML`.
