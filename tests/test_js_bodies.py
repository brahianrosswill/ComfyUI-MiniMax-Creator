"""The frontend loads, and all three node bodies actually mount.

Everything else in `tests/` checks what the backend builds. This checks the half
that runs in the browser, because the failure it exists for is silent from
Python's side and total from the user's: one throw anywhere in the module graph
and `app.registerExtension` never runs, so every node in the pack renders as its
raw widgets and nothing says why.

That has now happened twice for reasons no syntax check could catch — the CSS
lives in template literals (one per module under `js/minimax_creator/styles/`,
concatenated by `styles.js`), so a backtick inside a CSS comment ends the string
and turns the rest of the stylesheet into code that still parses. `node --check`
passes; the extension is dead.

So this imports the extension for real, against a DOM small enough to write down
(`dom.mjs`, generated below) and stubs for the three ComfyUI modules the pack
imports. Then it builds each node's body and reads the rendered text back. It is
not a rendering test — the shim has no layout and no CSS — it answers "did it
mount, and is the expected furniture in it".

    python3 tests/test_js_bodies.py

Skips itself if node is not installed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if shutil.which("node") is None:
    print("skipped: node is not installed")
    sys.exit(0)

# The smallest DOM the node bodies touch. Hand-written rather than jsdom so the
# suite keeps its "no dependencies" rule; every method here is one the pack
# actually calls, and an unimplemented one fails loudly rather than silently.
DOM = """
class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.style = {}; this.attrs = {};
    this.className = ""; this.textContent = ""; this.listeners = {}; this.isConnected = false;
    this.classList = { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false };
    this.dataset = {};
  }
  // A real input takes its starting value from the attribute, and the pack sets
  // it that way — el() has no special case for `value`.
  setAttribute(k, v) { this.attrs[k] = v; if (k === "value") this._value = v; }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] ??= []).push(fn); }
  removeEventListener() {}
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  append(...c) { c.forEach((x) => this.appendChild(x)); }
  replaceChildren(...c) { this.children = []; c.forEach((x) => this.appendChild(x)); }
  insertBefore(n) { return this.appendChild(n); }
  cloneNode() { return new Node(this.tagName); }
  remove() {}
  normalize() {}
  contains() { return false; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { top: 0, left: 0, width: 100, height: 100, bottom: 0, right: 0 }; }
  focus() {}
  scrollIntoView() {}
  get firstChild() { return this.children[0] ?? null; }
  get childNodes() { return this.children; }
  get nodeType() { return this.tagName === "#text" ? 3 : 1; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html ?? ""; }
  set value(v) { this._value = v; }
  get value() { return this._value ?? ""; }
  /** Everything rendered under this node, flattened — what the checks read. */
  get text() {
    return [this.textContent, ...this.children.map((c) => c.text ?? "")].join(" ");
  }
}
globalThis.document = {
  createElement: (tag) => new Node(tag),
  createElementNS: (ns, tag) => new Node(tag),
  createTextNode: (t) => Object.assign(new Node("#text"), { textContent: t }),
  body: new Node("body"),
  head: new Node("head"),
  documentElement: new Node("html"),
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
};
globalThis.window = { addEventListener() {}, removeEventListener() {},
                      getComputedStyle: () => ({}), innerWidth: 1600, innerHeight: 900,
                      devicePixelRatio: 1 };
globalThis.requestAnimationFrame = () => {};
globalThis.cancelAnimationFrame = () => {};
// The timeline lane measures itself to decide how much of each block's label
// fits — see TimelineBody.fitLane. Nothing in this DOM has a width, so the
// measure bails and the observer has nothing to report; it exists so that
// registering one is not a crash.
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.Image = class { set src(v) {} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
export const NodeClass = Node;
"""

STUBS = {
    "app.js": "export const app = { registerExtension: (e) => { globalThis.__ext = e; },"
              " graph: null, canvas: null };",
    # `fetchApi` answers the settings route the way the server does, and records
    # what was posted — which is what lets the settings page be exercised here.
    "api.js": """
globalThis.__posted = [];
let stored = { video_crf: 23, video_prefix: "minimax/renders/H3",
               image_prefix: "minimax/stills/prestage" };
export const api = {
  addEventListener() {}, removeEventListener() {}, apiURL: (u) => u,
  async fetchApi(route, options) {
    if (route.endsWith("/settings") && options?.method === "POST") {
      const patch = JSON.parse(options.body);
      globalThis.__posted.push(patch);
      stored = { ...stored, ...patch };
    }
    return { ok: true, status: 200, json: async () => ({ settings: stored }) };
  },
};
""",
    "widgets.js": "export const ComfyWidgets = {};",
}

CHECK = """
await import("./dom.mjs");
await import("./js/minimax_creator.js");
const S = await import("./js/minimax_creator/state.js");
const ext = globalThis.__ext;

const out = { registered: ext?.name ?? null, nodes: {}, still: null, errors: [] };

const fakeNode = (comfyClass, widgetName, blob) => ({
  comfyClass, id: 3, size: [400, 300], pos: [0, 0], title: comfyClass,
  widgets: [
    { name: widgetName, value: blob, type: "customtext", options: {}, computeSize: () => [0, 0] },
    { name: "seed", value: 0 }, { name: "steps", value: 20 }, { name: "cfg", value: 1 },
    { name: "sampler_name", value: "res_multistep" }, { name: "scheduler", value: "simple" },
  ],
  addDOMWidget(name, type, el) { this.dom = el; return { name, element: el }; },
  graph: { setDirtyCanvas() {}, _nodes: [], add() {} },
  properties: {},
});

for (const [cls, widget, blob] of [
  ["MiniMaxH3Creator", "creator_data", "{}"],
  ["MiniMaxH3Timeline", "timeline_data", "{}"],
  ["MiniMaxH3PreStage", "prestage_data", "{}"],
  ["MiniMaxH3PreStage", "prestage_data", JSON.stringify({ arch: "minimax" })],
]) {
  const node = fakeNode(cls, widget, blob);
  try {
    await ext.nodeCreated(node);
    const key = cls + (blob === "{}" ? "" : " (H3 still)");
    out.nodes[key] = { mounted: !!node.mmcBody && !!node.dom,
                       body: node.mmcBody?.editor?.constructor.name
                          ?? node.mmcBody?.constructor.name };
    if (blob !== "{}") out.still = node.mmcBody.root.text;
    if (cls === "MiniMaxH3Creator") out.creator = node.mmcBody.root.text;
  } catch (error) {
    out.errors.push(`${cls}: ${error.message}`);
  }
}

// A strip with supplied footage in it, on the node and in the modal.
//
// A clip card is not a generation and holds none of one's machinery — no
// assets, no prompt, no checkpoint — so every accessor the two renders call
// over the segments has to answer for it without asking it a sampler's
// question. One that does throws mid-render and takes the whole body with it,
// which is invisible from Python and total from the user's side: the strip
// stops redrawing and the card never appears.
try {
  const clipBlob = JSON.stringify({
    version: 2, render: "chained", prompt: "a corridor", aspect: "16:9", short_edge: 768,
    segments: [
      { prompt: "shot 1", duration_s: 5, assets: [], loras: [] },
      { kind: "clip", filename: "footage/take-3.mp4", duration_s: 12.5,
        width: 1920, height: 1080, continue: true, feather: 22 },
      { prompt: "shot 3", duration_s: 5, assets: [], loras: [], continue: true },
    ],
  });
  const node = fakeNode("MiniMaxH3Timeline", "timeline_data", clipBlob);
  await ext.nodeCreated(node);
  const body = node.mmcBody;
  out.clip = { mounted: !!node.dom, node: body.root.text };
  const { openTimeline } = await import("./js/minimax_creator/timeline.js");
  openTimeline({ timeline: body.timeline, onCommit: () => body.commit() });
  await new Promise((done) => setTimeout(done, 0));
  out.clip.modal = document.body.children.at(-1).text;
  // ...and the strip still redraws once something on it is touched, which is
  // the path an added clip actually takes.
  body.commit();
  out.clip.recommitted = body.root.children.length > 0;
} catch (error) {
  out.errors.push(`clip card: ${error.message}`);
}

// The pre-stage swaps its whole body when the model pill changes, which is the
// one place a rebuild can leave the node blank.
try {
  const node = fakeNode("MiniMaxH3PreStage", "prestage_data", JSON.stringify({ arch: "minimax" }));
  await ext.nodeCreated(node);
  const body = node.mmcBody;
  body.state.minimax.request.prompt = "a lighthouse";
  body.setArch("krea2");
  const image = body.editor.constructor.name;
  body.setArch("minimax");
  out.switch = { image, back: body.editor.constructor.name,
                 promptKept: body.state.minimax.request.prompt === "a lighthouse",
                 rendered: body.root.children.length > 0 };
} catch (error) {
  out.errors.push(`arch switch: ${error.message}`);
}

// The settings page: two tabs now, because where files land moved off the node
// bodies and onto this machine. Read the rendered tree rather than a screenshot
// — what matters is that both tabs exist, the folder fields carry the stored
// prefixes, and a committed edit posts the key the server expects.
try {
  const { openSettings } = await import("./js/minimax_creator/settings.js");
  openSettings();
  await new Promise((done) => setTimeout(done, 0));
  const page = document.body.children.at(-1);
  const tabs = [];
  const walk = (node) => {
    if (node.className === "mmc-tab") tabs.push(node.text.trim());
    (node.children ?? []).forEach(walk);
  };
  walk(page);
  out.settings = { tabs, quality: page.text.includes("crf 23") };

  // Switch to Folders and commit a new renders prefix, the way the field does.
  const tabButtons = [];
  const collect = (node) => {
    if (node.className === "mmc-tab") tabButtons.push(node);
    (node.children ?? []).forEach(collect);
  };
  collect(page);
  tabButtons[1].listeners.click[0]();
  const fields = [];
  const findFields = (node) => {
    if (node.className === "mmc-out-field") fields.push(node);
    (node.children ?? []).forEach(findFields);
  };
  findFields(page);
  out.settings.fields = fields.map((f) => f.value);
  fields[0].value = "client/shoot-3/take";
  fields[0].listeners.change[0]();
  await new Promise((done) => setTimeout(done, 0));
  out.settings.posted = globalThis.__posted;
} catch (error) {
  out.errors.push(`settings page: ${error.message}`);
}

console.log(JSON.stringify(out));
"""

work = tempfile.mkdtemp(prefix="mmc-js-")
try:
    # The pack imports ComfyUI's own modules from above its own directory, the
    # way the frontend serves them — so the copy keeps that shape: the js tree
    # inside a stand-in for the pack, and the stubs beside it.
    pack = os.path.join(work, "pack")
    shutil.copytree(os.path.join(ROOT, "js"), os.path.join(pack, "js"))
    os.makedirs(os.path.join(work, "scripts"), exist_ok=True)
    for name, source in STUBS.items():
        with open(os.path.join(work, "scripts", name), "w", encoding="utf-8") as handle:
            handle.write(source)
    for name, source in (("dom.mjs", DOM), ("check.mjs", CHECK)):
        with open(os.path.join(pack, name), "w", encoding="utf-8") as handle:
            handle.write(source)

    result = subprocess.run(["node", os.path.join(pack, "check.mjs")],
                            capture_output=True, text=True, cwd=pack)
finally:
    shutil.rmtree(work, ignore_errors=True)

if result.returncode != 0:
    # The whole point: a module-level throw takes the extension with it, and
    # this is where that shows up as a failure rather than as a dead canvas.
    print("the frontend did not load:\n" + (result.stderr.strip() or result.stdout.strip()))
    sys.exit(1)

report = json.loads(result.stdout.strip().splitlines()[-1])
FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


FAILURES.extend(report["errors"])
check("the extension registers", report["registered"], "minimax.creator")

# Each node's body, and which editor drives it. The H3 pre-stage is the one that
# differs: its still is a video generation, so it is driven by the Creator's own
# body rather than by the image-model editor beside it.
check("the Creator mounts", report["nodes"].get("MiniMaxH3Creator"),
      {"mounted": True, "body": "CreatorEditor"})
check("the Timeline mounts", report["nodes"].get("MiniMaxH3Timeline"),
      {"mounted": True, "body": "TimelineBody"})
check("the image pre-stage mounts", report["nodes"].get("MiniMaxH3PreStage"),
      {"mounted": True, "body": "PreStageEditor"})
check("the H3 pre-stage mounts the Creator's body",
      report["nodes"].get("MiniMaxH3PreStage (H3 still)"),
      {"mounted": True, "body": "CreatorEditor"})

# What a still is set up with. Every one of these is the video nodes' own
# control, reached by being a video request rather than by being re-described.
for wanted in ("Add image", "Add video", "Add audio", "Add LoRA", "Gallery", "From video",
               "Start frame", "End frame", "MiniMax H3", "latent", "T2VA"):
    if wanted not in (report["still"] or ""):
        FAILURES.append(f"the H3 still's body has no {wanted!r}")

# ...and what it must *not* have. The settings page is the video rate control;
# a node that writes PNGs offering it is a control over nothing.
for unwanted in ("Settings", " s ", "sweep"):
    if unwanted in (report["still"] or ""):
        FAILURES.append(f"the H3 still's body should not carry {unwanted!r}")
check("the Creator keeps the settings tool", "Settings" in (report["creator"] or ""), True)

# The settings page owns two questions now — how good the file is, and where it
# goes — so it has two tabs, and the folder fields are the only place the
# prefixes can be set.
settings = report.get("settings", {})
check("the settings page has both tabs", settings.get("tabs"), ["Quality", "Folders"])
check("the quality tab shows the encoder value", settings.get("quality"), True)
check("the folders tab carries both stored prefixes", settings.get("fields"),
      ["minimax/renders/H3", "minimax/stills/prestage"])
check("editing a folder writes it back under the server's own key",
      settings.get("posted"), [{"video_prefix": "client/shoot-3/take"}])

# Supplied footage: both renders survive it, and both say it is there.
clip = report.get("clip", {})
check("a timeline with a clip in it mounts", clip.get("mounted"), True)
check("the strip still redraws after a clip is committed", clip.get("recommitted"), True)
for wanted in ("clip", "take-3.mp4"):
    if wanted not in (clip.get("modal") or ""):
        FAILURES.append(f"the timeline modal does not name the clip's {wanted!r}")

check("switching to an image model rebuilds the body",
      report.get("switch", {}).get("image"), "PreStageEditor")
check("and switching back rebuilds it again",
      report.get("switch", {}).get("back"), "CreatorEditor")
check("the prompt survives the round trip",
      report.get("switch", {}).get("promptKept"), True)
check("the rebuilt body is not empty",
      report.get("switch", {}).get("rendered"), True)

if FAILURES:
    print(f"{len(FAILURES)} failure(s):")
    for failure in FAILURES:
        print("  -", failure)
    sys.exit(1)
print(f"the frontend loads and all {len(report['nodes'])} bodies mount")
