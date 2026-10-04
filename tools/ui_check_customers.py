"""Check the Customers tab in a real browser: headless Edge or Chrome, driven over the DevTools protocol.

    python tools/ui_check_customers.py --app http://127.0.0.1:8000            (the app running on a seeded DaaS database)
    python tools/ui_check_customers.py --app http://127.0.0.1:8000 --width 390 --shots ./shots

What it checks, each against the figures the API answers at the time (nothing here is a fixed number):
the tab renders with the headline tiles equal to /fleet/customers and /fleet/inflow; every row with a
customer code opens; the notes beside the grid and the defects table say what they must (landing is no
cover and names no code; the seeded zeros are the seed's; the current month is "to date"; the overdue
tile says how many more overdue devices the grid holds than the customers listed); each of the two reads
can fail alone and leaves the other half standing with a Retry; a failed customer detail loads again; a
render the user has left, or that a newer one replaced, paints nothing; no page error is thrown.

Failures are injected in the page by wrapping window.fetch; nothing on the server is changed. The script
logs in with the seeded admin (README: admin@example.com / admin) unless told otherwise.

Needs ``websocket-client`` (``pip install websocket-client``), which the app itself does not use, and a
Chromium-based browser (``--browser``, or found on PATH and in the usual Windows locations). Exits 1 when
a check fails.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    import websocket
except ImportError:                                   # a tool dependency, not an app dependency
    sys.exit("needs websocket-client: pip install websocket-client")

BROWSERS = ("msedge", "microsoft-edge", "google-chrome", "chromium", "chromium-browser", "chrome")
WINDOWS_BROWSERS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CODE_WORDS = ("order_mask", "_recommend", "inventory_position", "capacity_plan")   # identifiers that do not belong on the page


def find_browser(given: str | None) -> str:
    if given:
        return given
    for name in BROWSERS:
        if shutil.which(name):
            return shutil.which(name)
    for path in WINDOWS_BROWSERS:
        if os.path.isfile(path):
            return path
    sys.exit("no Edge or Chrome found: pass --browser <path>")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the Customers tab in a headless browser.")
    ap.add_argument("--app", default="http://127.0.0.1:8000", help="base URL of the running app")
    ap.add_argument("--browser", help="path of msedge or chrome (default: found on PATH or in the usual places)")
    ap.add_argument("--user", default="admin@example.com")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--width", type=int, default=1440, help="viewport width in CSS pixels (390 for a phone)")
    ap.add_argument("--shots", help="folder for full-page screenshots (none taken without it)")
    args = ap.parse_args(argv)

    app, width = args.app.rstrip("/"), args.width
    port = free_port()
    profile = tempfile.mkdtemp(prefix="ui-check-")
    if args.shots:
        os.makedirs(args.shots, exist_ok=True)
    results: list[tuple[str, bool]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

    proc = subprocess.Popen([find_browser(args.browser), "--headless=new", f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
                             f"--window-size={width},1200", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.5)
        if page is None:
            sys.exit("the browser did not open its DevTools port")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=180, suppress_origin=True)
        mid = [0]
        errors: list[str] = []

        def call(method, **params):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("method") == "Runtime.exceptionThrown":
                    d = msg["params"]["exceptionDetails"]
                    errors.append(d.get("exception", {}).get("description") or d.get("text"))
                if msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                    errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"]))
                if msg.get("id") == mid[0]:
                    return msg.get("result", {})

        def js(expr):
            r = call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
            if "exceptionDetails" in r:
                raise RuntimeError(r["exceptionDetails"])
            return r.get("result", {}).get("value")

        def wait(expr, secs=90):
            t0 = time.time()
            while time.time() - t0 < secs:
                if js(expr):
                    return round(time.time() - t0, 1)
                time.sleep(0.5)
            return None

        def metrics(height=1200):
            call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=width < 600)

        def shot(name):
            if not args.shots:
                return
            metrics(int(js("document.documentElement.scrollHeight")))
            time.sleep(0.5)
            data = call("Page.captureScreenshot", format="png", captureBeyondViewport=True)["data"]
            with open(os.path.join(args.shots, f"customers-{width}-{name}.png"), "wb") as fh:
                fh.write(base64.b64decode(data))
            metrics()

        def text(sel):
            return js(f"(document.querySelector({json.dumps(sel)}) || {{}}).innerText || ''")

        def tiles():
            return js("[...document.querySelectorAll('#screen .stat')].map(s => s.innerText.replace(/\\n/g, ' | '))")

        settled = "!!document.querySelector('#screen .cu-row') && !document.querySelector('#screen .cu-reload')"

        call("Runtime.enable")
        call("Page.enable")
        metrics()
        call("Page.navigate", url=app + "/")
        time.sleep(2)
        ok = js(f"""fetch('/api/v1/auth/login', {{method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                   body: new URLSearchParams({{username: {json.dumps(args.user)}, password: {json.dumps(args.password)}}})}})
                   .then(r => r.json()).then(d => {{ localStorage.setItem('scm_token', d.access_token || ''); return !!d.access_token; }})""")
        check("login", ok)
        if not ok:
            return 1
        # a full load straight onto the tab, so no other tab's render is still in flight when the checks start
        call("Page.navigate", url=app + "/?fresh=1#customers")
        t = wait("!!document.querySelector('#screen .cu-row')", 120)
        check("0 boot lands on the Customers tab and renders it", t is not None and js("currentTab") == "customers", f"{t} s")

        # what the page must show, read from the API now, formatted by the page's own num()
        cust = js("api('/fleet/customers')")
        inflow = js("api('/fleet/inflow')")
        tot = cust["totals"]
        fmt = lambda n: js(f"num({json.dumps(n)})")  # noqa: E731
        elsewhere = max(0, inflow["returns_overdue"] - tot["returns_overdue"])

        # the fetch wrapper: a path matching window.__fail gets a 500, one matching window.__slow waits first, one
        # matching window.__patch.re has its JSON answer rewritten by window.__patch.fn; all decided when the request
        # is made, so a later change of the flags does not touch it
        js("""(() => { if (window.__wrapped) return true; const real = window.fetch.bind(window); window.__wrapped = true;
              window.fetch = async (url, opts) => { const p = String(url).split('?')[0];
                const slow = !!(window.__slow && window.__slow.test(p)), fail = !!(window.__fail && window.__fail.test(p)), ms = window.__slowMs || 4000;
                const patch = window.__patch && window.__patch.re.test(p) ? window.__patch.fn : null;
                if (slow) await new Promise((r) => setTimeout(r, ms));
                if (fail) return new Response(JSON.stringify({detail: 'injected failure'}), {status: 500, headers: {'Content-Type': 'application/json'}});
                const res = await real(url, opts);
                if (!patch || !res.ok) return res;
                return new Response(JSON.stringify(patch(await res.json())), {status: res.status, headers: {'Content-Type': 'application/json'}}); };
              return true; })()""")

        # 1. the normal render
        js("showTab('customers')")
        t = wait(settled, 120)
        check("1 the tab renders", t is not None, f"{t} s")
        st = tiles()
        print("   tiles:", "  ##  ".join(st))
        want = [("Rented out now", tot["rented_total"]), ("Customers holding them", tot["customers_holding_devices"]),
                ("Back in 90 days", tot["contracts_ending_90d"]), ("Overdue returns", tot["returns_overdue"]),
                ("Inbound on open POs", inflow["inbound_open_total"])]
        check("1 each tile shows the API's figure",
              len(st) == 5 and all(s.startswith(label) and f"| {fmt(n)} |" in s for s, (label, n) in zip(st, want)))
        rows, clickable = js("document.querySelectorAll('#screen .cu-row').length"), js("document.querySelectorAll('#screen .cu-row.clickable').length")
        coded = sum(1 for r in cust["rows"] if r["customer_code"])
        check("1 one row per customer, every row with a code opens", rows == len(cust["rows"]) and clickable == coded,
              f"{rows} rows, {clickable} clickable, {coded} with a code")
        foot = js("[...document.querySelectorAll('#screen .wh-foot')].map(e => e.innerText).join(' ')")
        check("1 landing: no cover, which model counts the returns, no code names",
              "Landing is the volume that physically arrives" in foot and "the capacity plan does count returns" in foot
              and not any(w in foot for w in CODE_WORDS))
        check("1 the seeded zeros are named as the seed's", "property of the simulated data, not of the business" in foot
              and "at least eight days before that rental" in foot)
        cur = js("[...document.querySelectorAll('#screen .cu-grid tbody tr')].filter(r => r.innerText.includes('to date')).length")
        check("1 the current month of the defects table reads 'to date'", cur == 1, f"{cur} rows")
        overdue = st[3] if len(st) > 3 else ""
        check("1 the overdue tile counts the customers below and says how many more the grid holds",
              "at the customers below" in overdue
              and (f"{fmt(elsewhere)} more at organisations not listed" in overdue if elsewhere else "organisations not listed" not in overdue),
              f"{elsewhere} more in the grid")
        hints = js("[...document.querySelectorAll('#screen .stat__hint')].map(e => e.innerText).join(' ')")
        check("1 no em dash in the notes and the tile hints", "\u2014" not in foot and "\u2014" not in hints)
        sw = js("document.documentElement.scrollWidth")
        bar = js("(document.querySelector('.topbar') || {scrollWidth: 0}).scrollWidth")
        if bar > width:     # the app shell, not this tab: no tab can fit a viewport its top bar does not fit
            print(f"SKIP 1 the page fits the viewport: the app's top bar alone is {bar} px wide at {width} px (page {sw} px)")
        else:
            check("1 the page fits the viewport", sw <= width, f"{sw} px")
        shot("1-page")

        # 2. a customer opens
        js("document.querySelector('#screen .cu-row.clickable').click()")
        t = wait("!!document.querySelector('#screen .cu-detail:not(.hidden) .wh-cnt')", 60)
        check("2 a customer opens", t is not None, f"{t} s")
        shot("2-detail")

        # 3. /fleet/customers fails alone: the grid, the defects and the inbound tile stand
        js("window.__fail = /\\/fleet\\/customers$/; showTab('customers')")
        t = wait("!!document.querySelector('#screen .cu-grid') && !!document.querySelector('#screen .cu-reload')", 120)
        check("3 customers read fails: the grid still renders, with a Retry", t is not None, f"{t} s")
        st = tiles()
        check("3 the customer tiles say not loaded, the inbound tile has its figure",
              sum("not loaded" in s for s in st) == 4 and f"| {fmt(inflow['inbound_open_total'])} |" in st[4], "  ##  ".join(st))
        check("3 no customer rows", js("document.querySelectorAll('#screen .cu-row').length") == 0)
        shot("3-customers-failed")
        js("window.__fail = null; document.querySelector('#screen .cu-reload').click()")
        t = wait(settled, 120)
        check("3 Retry loads the whole tab again", t is not None, f"{t} s")

        # 4. /fleet/inflow fails alone: the customers table and its tiles stand
        js("window.__fail = /\\/fleet\\/inflow$/; showTab('customers')")
        t = wait("!!document.querySelector('#screen .cu-row') && document.querySelectorAll('#screen .cu-reload').length === 2", 120)
        check("4 inflow read fails: the customers table renders, grid and defects offer Retry", t is not None, f"{t} s")
        check("4 no month grid", js("document.querySelectorAll('#screen .cu-grid').length") == 0)
        st = tiles()
        check("4 the inbound tile says not loaded, the customer tiles have their figures",
              sum("not loaded" in s for s in st) == 1 and f"| {fmt(tot['contracts_ending_90d'])} |" in st[2]
              and "organisations not listed" not in st[3], "  ##  ".join(st))
        shot("4-inflow-failed")
        js("window.__fail = null")

        # 5. a failed detail loads again: by its button, and by closing and opening the row
        if coded >= 3:
            js("showTab('customers')")
            wait(settled, 120)
            a, b = js("[...document.querySelectorAll('#screen .cu-row.clickable')].slice(1, 3).map(r => r.dataset.i)")
            row = lambda i: f"document.querySelector('#screen .cu-row[data-i=\"{i}\"]')"  # noqa: E731
            js(f"window.__fail = /\\/fleet\\/customers\\/[^/]+$/; {row(a)}.click()")
            t = wait(f"!!document.querySelector('#cu-detail-{a} .cu-retry')", 60)
            check("5 a failed detail shows Retry", t is not None)
            check("5 a failed detail is not marked loaded", js(f"!document.querySelector('#cu-detail-{a}').dataset.loaded"))
            js(f"window.__fail = null; document.querySelector('#cu-detail-{a} .cu-retry').click()")
            t = wait(f"!!document.querySelector('#cu-detail-{a} .wh-cnt')", 60)
            check("5 Retry loads the detail", t is not None, f"{t} s")
            js(f"window.__fail = /\\/fleet\\/customers\\/[^/]+$/; {row(b)}.click()")
            wait(f"!!document.querySelector('#cu-detail-{b} .cu-retry')", 60)
            js(f"window.__fail = null; {row(b)}.click()")      # close
            js(f"{row(b)}.click()")                            # open again
            t = wait(f"!!document.querySelector('#cu-detail-{b} .wh-cnt')", 60)
            check("5 closing and opening a failed row loads it", t is not None, f"{t} s")
        else:
            print("SKIP 5 needs three customers with a code")

        # 6. leave the tab while it loads: a render that answers late (12 s, and failing, so it would paint failure
        #    notes) must not paint over the Returns tab the user went to
        js("window.__slowMs = 12000; window.__slow = /\\/fleet\\/(inflow|customers)$/; window.__fail = window.__slow; showTab('customers');"
           "setTimeout(() => { window.__slow = null; window.__fail = null; showTab('returns'); }, 300)")
        time.sleep(16)
        title_after = text("#screen .pagehead__title")
        check("6 a late Customers render does not paint over the Returns tab",
              js("currentTab") == "returns" and title_after and title_after != "Customers and returns"
              and not js("!!document.querySelector('#screen .cu-reload, #screen .cu-row')"), f"title 16 s later {title_after!r}")
        # two renders of this tab in flight: the older answers last (12 s, failing) and must not paint over the newer one
        js("window.__slowMs = 12000; window.__slow = /\\/fleet\\/(inflow|customers)$/; window.__fail = window.__slow; showTab('customers');"
           "setTimeout(() => { window.__slow = null; window.__fail = null; showTab('customers'); }, 300)")
        time.sleep(16)
        check("6 the newest render of the tab is the one that stays",
              js("currentTab") == "customers" and js(settled))
        js("window.__slowMs = 4000")

        # 7. overdue contracts held outside the customers listed (an organisation flagged as a supplier or a
        #    manufacturer): the grid counts them, the tile counts the customers and says how many more there are
        hint = "#screen .stat:nth-child(4) .stat__hint"
        js("window.__patch = {re: /\\/fleet\\/inflow$/, fn: (d) => Object.assign(d, {returns_overdue: d.returns_overdue + 3})};"
           "showTab('customers')")
        wait(settled, 120)
        got = text(hint)
        check("7 three more overdue in the grid: the tile says so", f"{fmt(elsewhere + 3)} more at organisations not listed" in got, got)
        js("window.__patch = {re: /\\/fleet\\/customers$/, fn: (d) => (d.totals.returns_overdue = 0, d)}; showTab('customers')")
        wait(settled, 120)
        got, cls = text(hint), js(f"document.querySelector({json.dumps(hint)}).className")
        check("7 none overdue at the customers, some elsewhere: on time below, the rest named, marked negative",
              got.startswith("every return on time at the customers below")
              and f"{fmt(inflow['returns_overdue'])} more at organisations not listed" in got and "stat__hint--neg" in cls,
              f"{got} / {cls}")
        js("window.__patch = null")

        check("no uncaught page errors", not errors, "; ".join(map(str, errors))[:500])
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    passed = sum(ok for _, ok in results)
    print(f"{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
