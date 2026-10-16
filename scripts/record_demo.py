"""Record the captioned demo video by driving the real app in a browser.

Start the app first (uvicorn looseends.web:app --port 7860), then:
    python scripts/record_demo.py docs/media/looseends-demo.mp4

Needs Playwright with Chromium, and ffmpeg for the MP4 conversion.
"""
import asyncio
import glob
import os
import shutil
import subprocess
import sys
import tempfile

from playwright.async_api import async_playwright

URL = os.environ.get("LOOSEENDS_URL", "http://127.0.0.1:7860")
W, H = 1600, 900

OVERLAY_JS = """
(() => {
  if (document.getElementById('demo-cap')) return;
  const st = document.createElement('style');
  st.textContent = `
    #demo-cap { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%); z-index: 9999; max-width: 1100px;
      background: rgba(20, 26, 52, 0.92); color: #fff; font: 500 25px/1.35 "Public Sans", sans-serif; padding: 14px 24px;
      border-radius: 12px; text-align: center; transition: opacity .25s; }
    #demo-cap:empty { opacity: 0; }
    #demo-cur { position: fixed; z-index: 10000; width: 22px; height: 22px; border-radius: 50%; pointer-events: none;
      background: rgba(168, 35, 65, .35); border: 2px solid #a82341; left: 800px; top: 450px;
      transition: left .7s cubic-bezier(.3,.7,.3,1), top .7s cubic-bezier(.3,.7,.3,1), transform .15s; transform: translate(-50%,-50%); }
    #demo-cur.down { transform: translate(-50%,-50%) scale(.7); }`;
  document.head.appendChild(st);
  const c = document.createElement('div'); c.id = 'demo-cap'; document.body.appendChild(c);
  const k = document.createElement('div'); k.id = 'demo-cur'; document.body.appendChild(k);
})();
"""

CARD = """<!doctype html><html><head><style>
@font-face {{ font-family: B; src: url({u}/static/fonts/bricolage-grotesque-latin-700-normal.woff2); }}
@font-face {{ font-family: P; src: url({u}/static/fonts/public-sans-latin-400-normal.woff2); }}
body {{ margin: 0; height: 100vh; display: grid; place-items: center; background: #eef1f4; color: #1a1f2e; font-family: P, sans-serif; }}
.w {{ max-width: 1100px; padding: 0 60px; }}
h1 {{ font: 700 72px/1.05 B, sans-serif; letter-spacing: -0.03em; margin: 0 0 24px; color: #1d2a6b; }}
p {{ font-size: 30px; line-height: 1.45; margin: 0 0 18px; }}
small {{ display: block; font-size: 18px; color: #5b6478; margin-top: 28px; }}
svg {{ width: 120px; height: 72px; margin-bottom: 18px; }}
code {{ font-family: P, sans-serif; background: #e4e8f6; padding: 2px 10px; border-radius: 6px; }}
</style></head><body><div class="w">{body}</div></body></html>"""

LOGO = ('<svg viewBox="0 0 40 24"><circle cx="10" cy="12" r="7" fill="none" stroke="#1d2a6b" stroke-width="3"/>'
        '<path d="M17 12c6 0 6-7 13-7 4 0 6 2 8 3" fill="none" stroke="#a82341" stroke-width="3" '
        'stroke-linecap="round" stroke-dasharray="3 3"/></svg>')


class Demo:
    def __init__(self, page):
        self.p = page

    async def overlay(self):
        await self.p.evaluate(OVERLAY_JS)

    async def say(self, text, hold=0.0):
        await self.p.evaluate("t => { const c = document.getElementById('demo-cap'); if (c) c.textContent = t; }", text)
        if hold:
            await self.p.wait_for_timeout(int(hold * 1000))

    async def move(self, selector, nth=0):
        box = await self.p.locator(selector).nth(nth).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await self.p.evaluate("([x, y]) => { const k = document.getElementById('demo-cur'); k.style.left = x + 'px'; k.style.top = y + 'px'; }", [x, y])
        await self.p.wait_for_timeout(800)
        return x, y

    async def click(self, selector, nth=0, after=0.8):
        await self.move(selector, nth)
        await self.p.evaluate("document.getElementById('demo-cur').classList.add('down')")
        await self.p.locator(selector).nth(nth).click()
        await self.p.wait_for_timeout(150)
        await self.p.evaluate("document.getElementById('demo-cur').classList.remove('down')")
        await self.p.wait_for_timeout(int(after * 1000))

    async def card(self, body, hold):
        await self.p.set_content(CARD.format(u=URL, body=body))
        await self.p.wait_for_timeout(int(hold * 1000))


def bench_numbers():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "benchmark", "results.json")) as f:
        r = json.load(f)
    m = r["methods"]
    return (r["hidden_links"], round(m["hybrid: graph + text"]["recall@10"] * 100),
            round(m["text (TF-IDF document search)"]["recall@10"] * 100))


async def record(out_path):
    hidden, hybrid, text = bench_numbers()
    tmp = tempfile.mkdtemp()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": W, "height": H}, record_video_dir=tmp,
                                        record_video_size={"width": W, "height": H})
        page = await ctx.new_page()
        d = Demo(page)

        await d.card(f"{LOGO}<h1>LooseEnds</h1><p>A company brain that finds knowledge which has quietly died.</p>"
                     "<small>Graph Hacks 2026, Company Brain track. Built on FalkorDB.</small>", 4.5)
        await d.card("<p>Company knowledge rarely goes missing. It goes <b>stale</b> when a decision changes and the work "
                     "built on it doesn't. It gets <b>orphaned</b> when its owner leaves. Promises pass their deadline "
                     "and nobody notices.</p><p>Search can't see this, because the problem lives in the connections "
                     "between records, not inside any one document.</p>", 9)

        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        await d.overlay()
        await d.say("The data: 679 real Kubernetes design decisions, 722 people and 38 teams, loaded into FalkorDB.", 4.5)
        await d.move("#status")
        await d.say("Three detectors run as Cypher queries over the graph. Each finding comes with the path that proves it.", 4.5)

        await d.click("#kinds button[data-kind=stale]")
        await d.say("Stale: open decisions that still rest on one that was retired.", 3)
        await d.click('.items button[data-detail="5547"]', after=1.5)
        await d.say("KEP-5547 still depends on KEP-5832, which Gang Scheduling (KEP-4671) has replaced.", 4.5)
        await d.move("#cy")
        await d.say("Dashed red rings are retired decisions. Red threads mean 'replaces'. Blue threads are dependencies.", 5)
        await d.move("#p-decision .people")
        await d.say("It also ranks who could take over, from who reviewed it and who works on linked decisions.", 5)

        await d.say("What if a decision changes? Pick it and LooseEnds walks every thread up to three hops.", 3.5)
        await d.click("#q")
        await page.keyboard.type("583", delay=120)
        await page.wait_for_timeout(600)
        await d.click("#results button", after=1.8)
        await d.say("Changing Coscheduling (KEP-583) would affect 6 open decisions, each with its exact path and owners.", 5.5)

        await d.click("#kinds button[data-kind=broken_promises]", after=1)
        await d.say("Missed targets: promises to reach a stage by a release that has already shipped.", 3.5)
        await d.click(".items li:first-child button", after=1.5)
        await d.say("KEP-1845 promised beta by v1.21. It is still alpha, 17 releases later, and its author has gone quiet.", 5.5)

        await d.click("#panels button[data-panel=ask]", after=0.5)
        await d.say("The agent answers questions by calling the same graph tools. You can see every call it made.", 3)
        await d.click("#ask")
        await page.keyboard.type("Who can take over KEP-1845?", delay=45)
        await d.click("#askform button[type=submit]", after=1.5)
        await d.click(".msg.a:last-of-type .trace summary", after=0.5)
        await d.say("Answers only use what the graph returned, and say how they got there.", 4.5)

        await d.click("#ask")
        await page.keyboard.type("Draft a follow-up for KEP-1845", delay=45)
        await d.click("#askform button[type=submit]", after=1.5)
        await d.say("It can act: the follow-up is written to the graph as a draft, addressed to someone still active.", 5)
        await d.click("#panels button[data-panel=followups]", after=1)
        await d.say("Nothing is sent until a person approves it.", 2.5)
        await d.click("#p-followups button[data-status=approved]", after=1.2)

        await d.click("#panels button[data-panel=benchmark]", after=1)
        await d.say(f"Does the graph help? We hid all {hidden} declared links between decisions and asked each method to find them.", 5.5)
        await d.move(".bars li.best")
        await d.say(f"Graph plus text finds {hybrid}% of hidden links in its top 10. Text search alone finds {text}%.", 5.5)
        await d.say("The same method suggests undeclared links in the app, always marked as unconfirmed.", 4)
        await d.say("")

        await d.card("<p>Other agents can use LooseEnds too. It runs as an MCP server, over stdio or HTTP at "
                     "<code>/mcp</code>, with tools such as <code>impact_of</code>, <code>who_knows</code>, "
                     "<code>in_force</code> and <code>draft_followup</code>.</p>"
                     "<p>So a coding agent can ask what breaks before it changes something.</p>", 8)
        await d.card(f"{LOGO}<h1>LooseEnds</h1><p>FalkorDB, Cypher, PageRank and full-text search over a real "
                     "organisation's decisions.</p><small>github.com/aayushsinghthapa-cloud/looseends</small>", 5)

        video = page.video
        await ctx.close()
        await browser.close()
        webm = await video.path()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", webm, "-c:v", "libx264", "-preset", "slow",
                    "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print("Wrote", out_path)


if __name__ == "__main__":
    asyncio.run(record(sys.argv[1] if len(sys.argv) > 1 else "docs/media/looseends-demo.mp4"))
