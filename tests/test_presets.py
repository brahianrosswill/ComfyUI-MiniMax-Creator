"""A preset put back is the setup it was taken from.

Two questions, and neither has a Python half to mirror — presets live entirely on
the near side of the queue, which is the one nice thing about the feature.

**Round trip.** Capture a piece, apply every section to a node that has nothing
in common with it, serialise: the blob has to come back identical to the one that
was captured. That is the whole promise, and it is the one a hand-written
per-section apply gets wrong quietly — a field nobody thought to carry reads as
"the preset did not set that", which is indistinguishable from "the preset set it
to the default" right up until somebody's two-pass render comes out at 720.

The sampler row is checked with it, because it is not in the blob and a preset
that dropped it would still pass a blob comparison.

**Cross-scope.** A preset of one kind applied to a node of another lands exactly
the sections that can cross and refuses the rest *with a reason*. The refusals
are the interesting half: a section that cannot cross must be visible and
explained rather than missing.

    python3 tests/test_presets.py

Skips itself if node is not installed. Shares the stub tree with
`test_js_bodies.py`, minus the DOM — nothing here renders anything.
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

# `presets.js` reaches ComfyUI's api for the userdata calls and `i18n.js` reaches
# the setting store for the locale. Neither is exercised here — capture and apply
# are pure — so the stubs only have to exist.
STUBS = {
    "app.js": "export const app = { registerExtension() {}, extensionManager: null };",
    "api.js": """
const store = new Map();
export const api = {
  apiURL: (u) => u,
  async fetchApi() { return { ok: true, status: 200, json: async () => ({}) }; },
  async getUserData(file) {
    return store.has(file)
      ? { status: 200, json: async () => JSON.parse(store.get(file)) }
      : { status: 404, json: async () => null };
  },
  async storeUserData(file, value) { store.set(file, JSON.stringify(value)); return { status: 200 }; },
  async deleteUserData(file) { store.delete(file); return { status: 204 }; },
};
""",
    "widgets.js": "export const ComfyWidgets = {};",
}

CHECK = r"""
const S = await import("./js/minimax_creator/state.js");
const P = await import("./js/minimax_creator/presets.js");

const out = { errors: [] };

/** A stand-in for the node's sampler widgets: the same {value, set} pair
 *  `sampling.widgetIO` hands the row, over a plain object. */
function fakeIO(initial = {}) {
  const values = { ...initial };
  return {
    values,
    value: (name, fallback) => (name in values ? values[name] : fallback),
    set: (name, value) => { values[name] = value; },
  };
}

const ROW = {
  steps: 7, cfg: 2.5, sampler_name: "euler", scheduler: "beta",
  shift_video: 6, shift_audio: 4, block_cache: "fast",
  spectrum: true, spectrum_blend: 0.75,
  // Not a preset's business, and the check below proves it is not carried.
  seed: 4471,
};

// A piece with something in every section, so nothing can pass by being empty.
const SOURCE = JSON.stringify({
  version: 2,
  render: "chained",
  prompt: "the standing description",
  soundscape: "wind over stone",
  music: "low strings",
  aspect: "9:16",
  short_edge: 480,
  upscale: "direct",
  sample_edge: 640,
  refine_denoise: 0.7,
  audio_tail_s: 2.5,
  loras: [{ name: "turbo/lightx2v.safetensors", strength: 0.6 }],
  assets: [{ handle: "ref-1", kind: "image", role: "reference", filename: "plate.png" }],
  models: { fl2va: "fl2va.safetensors", clip: "clip.safetensors", vae: "vae.safetensors",
            audio_vae: "audio.safetensors", route: "ref2va", dtype: "fp8_e4m3fn" },
  turbo: { lora: "turbo/lightx2v.safetensors", quality: "draft", on: true,
           saved: { steps: 20, sampler_name: "res_multistep", scheduler: "simple",
                    shift_video: 12, shift_audio: 3 } },
  segments: [
    { prompt: "shot one @ref-1", assets: [], loras: [], duration_s: 5, checkpoint: "ref2va" },
    { prompt: "shot two", assets: [], loras: [], duration_s: 7, merge: true },
    { prompt: "shot three", assets: [
        { handle: "img-1", kind: "image", role: "first_frame", filename: "open.png" }],
      loras: [], duration_s: 9, continue: true, continue_audio: true, feather: 22 },
  ],
});

// ---- round trip -------------------------------------------------------------

try {
  const source = S.parseTimeline(SOURCE);
  S.syncTimeline(source);
  const io = fakeIO(ROW);
  const body = P.capturePiece(source, io);
  const captured = S.serializeTimeline(source);

  // A node with nothing in common: different canvas, different weights,
  // different strip. Every field the preset carries has to overwrite one.
  const target = S.parseTimeline(JSON.stringify({
    version: 2, prompt: "something else entirely", aspect: "1:1", short_edge: 720,
    models: { fl2va: "other.safetensors" },
    segments: [{ prompt: "a card that should not survive", assets: [], loras: [], duration_s: 6 }],
  }));
  const targetIO = fakeIO({ steps: 20, cfg: 1, sampler_name: "res_multistep",
                            scheduler: "simple", seed: 99 });
  P.applyToPiece(body, Object.keys(body), target, targetIO);
  S.syncTimeline(target);

  out.roundTrip = {
    blob: S.serializeTimeline(target) === captured,
    // Every widget the row carries, at the value it was captured at.
    row: P.SPEED_WIDGETS.every((name) => targetIO.values[name] === ROW[name]),
    // …and the seed left exactly as the target had it.
    seedUntouched: targetIO.values.seed === 99,
  };
  if (!out.roundTrip.blob) {
    out.roundTrip.got = JSON.parse(S.serializeTimeline(target));
    out.roundTrip.want = JSON.parse(captured);
  }
} catch (error) {
  out.errors.push(`round trip: ${error.stack}`);
}

// A section left out is a section left alone: applying only the look must not
// touch the prompt, the strip or anything else.
try {
  const source = S.parseTimeline(SOURCE);
  S.syncTimeline(source);
  const body = P.capturePiece(source, fakeIO(ROW));

  const target = S.parseTimeline(JSON.stringify({
    version: 2, prompt: "keep me", aspect: "1:1", short_edge: 720, models: {},
    segments: [{ prompt: "keep me too", assets: [], loras: [], duration_s: 6 }],
  }));
  const io = fakeIO({ steps: 20 });
  P.applyToPiece(body, ["look"], target, io);
  S.syncTimeline(target);
  out.partial = {
    lookLanded: target.aspect === "9:16" && target.short_edge === 480
             && target.upscale === "direct" && target.refine_denoise === 0.7,
    promptKept: target.prompt === "keep me",
    stripKept: target.segments.length === 1 && target.segments[0].prompt === "keep me too",
    rowKept: io.values.steps === 20,
  };
} catch (error) {
  out.errors.push(`partial: ${error.stack}`);
}

// A look that never left the defaults still puts a node that did back. The blob
// omits a field at its default, so a naive merge would leave the target's.
try {
  const plain = S.parseTimeline(JSON.stringify({
    version: 2, prompt: "", models: {},
    segments: [{ prompt: "", assets: [], loras: [], duration_s: 6 }],
  }));
  S.syncTimeline(plain);
  const body = P.capturePiece(plain, fakeIO({}));
  const target = S.parseTimeline(SOURCE);
  S.syncTimeline(target);
  P.applyToPiece(body, ["look"], target, fakeIO({}));
  const fresh = S.emptyTimeline();
  out.defaults = {
    aspect: target.aspect === fresh.aspect,
    upscale: target.upscale === fresh.upscale,
    denoise: target.refine_denoise === fresh.refine_denoise,
  };
} catch (error) {
  out.errors.push(`defaults: ${error.stack}`);
}

// ---- shots ------------------------------------------------------------------

try {
  const source = S.parseTimeline(SOURCE);
  S.syncTimeline(source);
  const io = fakeIO(ROW);
  const body = P.captureShot(source, 2, io);

  const target = S.parseTimeline(SOURCE);
  S.syncTimeline(target);
  P.applyToShot(body, Object.keys(body), target.segments[0], fakeIO({}));
  S.syncTimeline(target);
  const landed = target.segments[0];
  // Read off the serialized blob, not off the live object: what a card *is* is
  // what `serializeTimeline` writes for it, and a leftover field the serializer
  // refuses to emit is not a seam.
  const written = JSON.parse(S.serializeTimeline(target)).segments[0];
  out.shot = {
    prompt: landed.prompt === "shot three",
    duration: landed.duration_s === 9,
    // Segment 1 has nothing in front of it, so `syncTimeline` clears the seam
    // the preset carried — the normaliser doing exactly its job rather than a
    // preset writing a state the editor could not produce.
    seamPruned: written.continue === undefined && written.continue_audio === undefined
             && written.feather === undefined,
    frameCarried: (landed.assets ?? []).some((a) => a.role === "first_frame"
                                              && a.filename === "open.png"),
  };
  // …and onto a card that *does* have something in front of it, where the seam
  // is legal and has to survive.
  const second = S.parseTimeline(SOURCE);
  S.syncTimeline(second);
  P.applyToShot(body, Object.keys(body), second.segments[1], fakeIO({}));
  S.syncTimeline(second);
  out.shot.seamKept = second.segments[1].continue === true
                   && second.segments[1].feather === 22;
} catch (error) {
  out.errors.push(`shot: ${error.stack}`);
}

// ---- cross-scope ------------------------------------------------------------

try {
  const reasons = {};
  for (const [key, from, to, opts] of [
    ["strip", "piece", "prestage", {}],
    ["strip", "piece", "shot", {}],
    ["look", "piece", "shot", {}],
    ["weights", "piece", "shot", {}],
    ["shot", "shot", "piece", {}],
    ["weights", "piece", "prestage", { targetArch: "krea2" }],
  ]) {
    const verdict = P.crossable(key, from, to, opts);
    reasons[`${key}:${from}->${to}`] = verdict.ok ? true : verdict.why;
  }
  out.refusals = reasons;
  out.crossings = {
    // The one weights crossing that is legal: a pre-stage on the H3 branch runs
    // a creator request, under the same keys.
    weightsToH3: P.crossable("weights", "piece", "prestage", { targetArch: "minimax" }).ok,
    weightsFromH3: P.crossable("weights", "prestage", "piece", { arch: "minimax" }).ok,
    promptEverywhere: ["piece", "shot", "prestage"].every((to) =>
      P.crossable("prompt", "piece", to).ok),
    lorasEverywhere: ["piece", "shot", "prestage"].every((to) =>
      P.crossable("loras", "piece", to).ok),
  };
  // Every refusal says something. An empty reason is a disabled row with no
  // explanation on it, which is the failure mode this design set out to avoid.
  out.everyRefusalExplained = Object.values(reasons)
    .every((why) => why === true || (typeof why === "string" && why.length > 12));
} catch (error) {
  out.errors.push(`cross: ${error.stack}`);
}

// A pre-stage's init becomes a card's start frame — the direction the whole
// pre-stage/creator pairing exists for.
try {
  const pre = S.parsePreStage(JSON.stringify({
    version: 1, arch: "krea2", prompt: "a lighthouse",
    aspect: "3:2", short_edge: 1024,
    init: { filename: "plate.png", denoise: 0.55 },
    refs: [{ filename: "style.png" }],
    loras: [{ name: "krea/style.safetensors", strength: 0.8 }],
    models: { krea2: {}, ideogram4: {}, minimax: {} },
  }));
  const body = P.capturePreStage(pre, fakeIO({ steps: 52, cfg: 3.5 }));

  const target = S.parseTimeline(SOURCE);
  S.syncTimeline(target);
  const card = target.segments[0];
  P.applyToShot(body, ["prompt", "refs", "loras"], card, fakeIO({}), { from: "prestage" });
  S.syncTimeline(target);
  out.preToShot = {
    prompt: card.prompt === "a lighthouse",
    init: (card.assets ?? []).some((a) => a.role === "first_frame" && a.filename === "plate.png"),
    ref: (card.assets ?? []).some((a) => a.role === "reference" && a.filename === "style.png"),
    handlesUnique: new Set((card.assets ?? []).map((a) => a.handle)).size
                 === (card.assets ?? []).length,
    lora: (card.loras ?? []).length === 1,
  };
} catch (error) {
  out.errors.push(`prestage -> shot: ${error.stack}`);
}

// The H3 branch keeps its checkpoints in `minimax.request.models`, not in the
// top-level `models` block — that one is filled for the two image architectures
// only. A capture that read the wrong one looked fine and quietly carried no
// weights at all, which on apply *blanked* the target's.
try {
  const h3 = S.parsePreStage(JSON.stringify({
    version: 1, arch: "minimax", prompt: "a still",
    aspect: "16:9", short_edge: 720,
    models: { krea2: {}, ideogram4: {}, minimax: {} },
    minimax: {
      frames: 5, latent_index: 0,
      request: { prompt: "a still", assets: [], loras: [], aspect: "16:9", short_edge: 720,
                 models: { fl2va: "fl2va.safetensors", ref2va: "ref2va.safetensors",
                           clip: "clip.safetensors", vae: "vae.safetensors",
                           audio_vae: "audio.safetensors", route: "ref2va" } },
    },
  }));
  const body = P.capturePreStage(h3, fakeIO({}));

  // Onto a piece: the crossing `crossable` exists to allow.
  const piece = S.parseTimeline(JSON.stringify({
    version: 2, prompt: "", models: { fl2va: "wrong.safetensors" },
    segments: [{ prompt: "", assets: [], loras: [], duration_s: 6 }],
  }));
  P.applyToPiece(body, ["weights"], piece, fakeIO({}), { from: "prestage" });

  // …and back onto a pre-stage of its own kind.
  const back = S.parsePreStage(JSON.stringify({ version: 1, arch: "krea2" }));
  P.applyToPreStage(body, ["weights"], back, fakeIO({}), { from: "prestage" });

  out.h3Weights = {
    captured: (body.weights?.minimax?.models ?? {}).fl2va === "fl2va.safetensors",
    toPiece: piece.models.fl2va === "fl2va.safetensors"
          && piece.models.clip === "clip.safetensors"
          && piece.models.route === "ref2va",
    toPreStage: back.minimax.request.models.fl2va === "fl2va.safetensors"
             && back.minimax.request.models.audio_vae === "audio.safetensors",
    framesKept: back.minimax.frames === 5,
  };
} catch (error) {
  out.errors.push(`h3 weights: ${error.stack}`);
}

// References crossing *into* a pre-stage have to be re-handled. A ref with no
// handle draws as "@undefined" — and worse, the chip's remove button filters on
// `r.handle !== ref.handle`, so one undefined handle deletes every ref sharing
// it, which is all of them. The three-slot cap is the editor's, and a preset
// must not be able to exceed it.
try {
  const wide = S.parseTimeline(JSON.stringify({
    version: 2, prompt: "", models: {},
    segments: [{ prompt: "", loras: [], duration_s: 6, assets: [
      { handle: "img-1", kind: "image", role: "first_frame", filename: "open.png" },
      { handle: "img-2", kind: "image", role: "reference", filename: "r1.png" },
      { handle: "img-3", kind: "image", role: "reference", filename: "r2.png" },
      { handle: "img-4", kind: "image", role: "reference", filename: "r3.png" },
      { handle: "img-5", kind: "image", role: "reference", filename: "r4.png" },
      { handle: "vid-1", kind: "video", role: "reference", filename: "clip.mp4" },
    ] }],
  }));
  S.syncTimeline(wide);
  const shotBody = P.captureShot(wide, 0, fakeIO({}));
  const pre = S.parsePreStage(JSON.stringify({ version: 1, arch: "krea2" }));
  P.applyToPreStage(shotBody, ["refs"], pre, fakeIO({}), { from: "shot" });
  out.refsIntoPreStage = {
    init: pre.init?.filename === "open.png",
    capped: pre.refs.length === S.PRESTAGE_MAX_REFS,
    everyHandled: pre.refs.every((r) => typeof r.handle === "string" && r.handle.length > 0),
    handlesUnique: new Set(pre.refs.map((r) => r.handle)).size === pre.refs.length,
    // A video reference has nowhere to go on an image model.
    noVideo: pre.refs.every((r) => !r.filename.endsWith(".mp4")),
  };
} catch (error) {
  out.errors.push(`refs into prestage: ${error.stack}`);
}

// A pre-stage round trip of its own.
try {
  const pre = S.parsePreStage(JSON.stringify({
    version: 1, arch: "ideogram4", quality: "quality", prompt: "a poster",
    aspect: "3:2", short_edge: 1024, refs: [], loras: [],
    models: { krea2: {}, ideogram4: {}, minimax: {} },
  }));
  const io = fakeIO({ steps: 48, cfg: 7, sampler_name: "euler", scheduler: "simple" });
  const body = P.capturePreStage(pre, io);
  const captured = S.serializePreStage(pre);

  const target = S.parsePreStage(JSON.stringify({ version: 1, arch: "krea2", prompt: "other" }));
  const targetIO = fakeIO({ steps: 52, cfg: 3.5 });
  P.applyToPreStage(body, Object.keys(body), target, targetIO);
  out.preRoundTrip = {
    blob: S.serializePreStage(target) === captured,
    arch: target.arch === "ideogram4",
    row: targetIO.values.steps === 48 && targetIO.values.cfg === 7,
  };
  if (!out.preRoundTrip.blob) {
    out.preRoundTrip.got = JSON.parse(S.serializePreStage(target));
    out.preRoundTrip.want = JSON.parse(captured);
  }
} catch (error) {
  out.errors.push(`prestage round trip: ${error.stack}`);
}

// ---- the card ---------------------------------------------------------------

try {
  const source = S.parseTimeline(SOURCE);
  S.syncTimeline(source);
  const body = P.capturePiece(source, fakeIO(ROW));
  const card = P.describe(body, "piece");
  out.card = {
    // Shots two and three of the source share a pass (`merge: true` on the
    // second), so three cards draw as two casings.
    passes: card.lane.runs.length,
    blocks: card.lane.runs.reduce((n, run) => n + run.blocks.length, 0),
    // Real durations, not equal shares: the whole point of the lane.
    seconds: card.lane.runs.flatMap((run) => run.blocks.map((b) => b.seconds)),
    shots: card.facts.shots,
    total: card.facts.seconds,
    // Card 1 cites @ref-1 from the pool, card 3 has its own start frame, card 2
    // has neither and draws flat.
    frames: card.frames.map((f) => [f.at, f.path]),
  };
  // With a cover the lane is a ruler and draws no pictures, so the frames are
  // not collected at all.
  out.card.framesWithCover = P.describe(body, "piece", {
    cover: { path: "out.mp4 [output]", v: 1 } }).frames.length;
} catch (error) {
  out.errors.push(`card: ${error.stack}`);
}

// The cover has to record *which kind* of render it is, not just where it is:
// a still is served by core's /view as a webp, a clip only by this pack's thumb
// route. Point an <img> at an .mp4 and it renders nothing at all — which against
// the hero's near-black reads as a cover that is simply black.
try {
  out.cover = P.coverFromResult({
    isImage: false,
    saved: { filename: "H3_00021_.mp4", subfolder: "minimax/renders", type: "output" },
  });
  out.coverStill = P.coverFromResult({
    isImage: true,
    saved: { filename: "prestage_00003_.png", subfolder: "minimax/stills", type: "output" },
  });
  out.coverEmpty = P.coverFromResult(null);
  out.coverNoResult = P.coverFromResult({ isImage: false, saved: null });
} catch (error) {
  out.errors.push(`cover: ${error.stack}`);
}

// ---- storage ----------------------------------------------------------------

try {
  const source = S.parseTimeline(SOURCE);
  S.syncTimeline(source);
  const body = P.capturePiece(source, fakeIO(ROW));
  const row = await P.savePreset({ name: "  Portal walk  ", scope: "piece", data: body });
  const listed = await P.listPresets({ force: true });
  const readBack = await P.loadBody(row);
  out.storage = {
    named: row.name === "Portal walk",
    listed: listed.length === 1 && listed[0].id === row.id,
    // The index carries the whole card, so the grid never fetches a body to draw.
    cardInIndex: !!row.lane && !!row.facts && Array.isArray(row.frames),
    bodyRoundTrips: JSON.stringify(readBack) === JSON.stringify(body),
    starred: (await P.updatePreset(row.id, { starred: true })).starred === true,
  };
  await P.deletePreset(row.id);
  out.storage.deleted = (await P.listPresets({ force: true })).length === 0;
} catch (error) {
  out.errors.push(`storage: ${error.stack}`);
}

// The shipped starters load, describe themselves, and name no files.
try {
  const { BUILTIN } = await import("./js/minimax_creator/presets/builtin.js");
  out.builtin = {
    count: BUILTIN.length,
    allDescribed: BUILTIN.every((row) => !!row.facts && Array.isArray(row.sections)
                                      && row.sections.length > 0),
    scopesKnown: BUILTIN.every((row) => P.SCOPES.includes(row.scope)),
    // The rule that keeps a shipped library from being red on every machine but
    // the one it was written on.
    namesNoFiles: BUILTIN.every((row) => {
      const json = JSON.stringify(row.data);
      return !/safetensors|\.png|\.mp4|\.gguf/i.test(json);
    }),
    sectionsAllowed: BUILTIN.every((row) =>
      row.sections.every((key) => P.SCOPE_SECTIONS[row.scope].includes(key))),
  };
} catch (error) {
  out.errors.push(`builtin: ${error.stack}`);
}

console.log(JSON.stringify(out));
"""

work = tempfile.mkdtemp(prefix="mmc-presets-")
try:
    pack = os.path.join(work, "pack")
    shutil.copytree(os.path.join(ROOT, "js"), os.path.join(pack, "js"))
    os.makedirs(os.path.join(work, "scripts"), exist_ok=True)
    for name, source in STUBS.items():
        with open(os.path.join(work, "scripts", name), "w", encoding="utf-8") as handle:
            handle.write(source)
    with open(os.path.join(pack, "check.mjs"), "w", encoding="utf-8") as handle:
        handle.write(CHECK)
    result = subprocess.run(["node", os.path.join(pack, "check.mjs")],
                            capture_output=True, text=True, cwd=pack)
finally:
    shutil.rmtree(work, ignore_errors=True)

if result.returncode != 0:
    print("the preset module did not load:\n"
          + (result.stderr.strip() or result.stdout.strip()))
    sys.exit(1)

report = json.loads(result.stdout.strip().splitlines()[-1])
from harness import FAILURES, check, passed

FAILURES.extend(report["errors"])

# ---- round trip -------------------------------------------------------------

trip = report.get("roundTrip", {})
if not trip.get("blob"):
    FAILURES.append("a captured piece does not come back identical:\n"
                    f"    want {json.dumps(trip.get('want'), sort_keys=True)[:400]}\n"
                    f"    got  {json.dumps(trip.get('got'), sort_keys=True)[:400]}")
check("the sampler row comes back too — it is not in the blob", trip.get("row"), True)
check("...and the seed is left where the target had it", trip.get("seedUntouched"), True)

partial = report.get("partial", {})
check("applying one section lands it", partial.get("lookLanded"), True)
check("...and leaves the prompt alone", partial.get("promptKept"), True)
check("...and the strip", partial.get("stripKept"), True)
check("...and the sampler row", partial.get("rowKept"), True)

# The trap a naive merge falls into: a blob omits a field at its default, so a
# preset of defaults has to *reset* rather than say nothing.
defaults = report.get("defaults", {})
check("a default look resets an aspect that had moved", defaults.get("aspect"), True)
check("...the upscale mode", defaults.get("upscale"), True)
check("...and the refine denoise", defaults.get("denoise"), True)

# ---- shots ------------------------------------------------------------------

shot = report.get("shot", {})
check("a shot preset carries its prompt", shot.get("prompt"), True)
check("...its duration", shot.get("duration"), True)
check("...and its start frame", shot.get("frameCarried"), True)
check("a seam that cannot exist on card 1 is pruned rather than written",
      shot.get("seamPruned"), True)
check("...and kept where there is something in front of it", shot.get("seamKept"), True)

# ---- cross-scope ------------------------------------------------------------

crossings = report.get("crossings", {})
check("a piece's weights reach a pre-stage on the H3 branch", crossings.get("weightsToH3"), True)
check("...and come back from one", crossings.get("weightsFromH3"), True)
check("a prompt crosses to everything", crossings.get("promptEverywhere"), True)
check("so do LoRAs", crossings.get("lorasEverywhere"), True)

refusals = report.get("refusals", {})
for key in ("strip:piece->prestage", "strip:piece->shot", "look:piece->shot",
            "weights:piece->shot", "shot:shot->piece", "weights:piece->prestage"):
    if refusals.get(key) is True:
        FAILURES.append(f"{key} should not be allowed to cross")
check("every refusal carries a reason the row can show",
      report.get("everyRefusalExplained"), True)

pre_to_shot = report.get("preToShot", {})
check("a pre-stage's prompt reaches a card", pre_to_shot.get("prompt"), True)
check("...its init becomes that card's start frame", pre_to_shot.get("init"), True)
check("...its style refs become references", pre_to_shot.get("ref"), True)
check("...with handles that cannot collide", pre_to_shot.get("handlesUnique"), True)
check("...and its LoRAs come along", pre_to_shot.get("lora"), True)

h3 = report.get("h3Weights", {})
check("an H3 pre-stage's checkpoints are captured from the still's own request",
      h3.get("captured"), True)
check("...and reach a piece rather than blanking its weights", h3.get("toPiece"), True)
check("...and come back onto a pre-stage", h3.get("toPreStage"), True)
check("...without losing the still's frame settings", h3.get("framesKept"), True)

into = report.get("refsIntoPreStage", {})
check("a card's start frame becomes the pre-stage's init", into.get("init"), True)
check("references crossing in are capped at the encoder's three slots",
      into.get("capped"), True)
check("...and every one of them is handled", into.get("everyHandled"), True)
check("...uniquely, so removing one chip removes one", into.get("handlesUnique"), True)
check("a video reference has nowhere to go on an image model", into.get("noVideo"), True)

pre_trip = report.get("preRoundTrip", {})
if not pre_trip.get("blob"):
    FAILURES.append("a captured pre-stage does not come back identical:\n"
                    f"    want {json.dumps(pre_trip.get('want'), sort_keys=True)[:400]}\n"
                    f"    got  {json.dumps(pre_trip.get('got'), sort_keys=True)[:400]}")
check("the architecture comes with it", pre_trip.get("arch"), True)
check("and so does its shorter row", pre_trip.get("row"), True)

# ---- the card ---------------------------------------------------------------

card = report.get("card", {})
check("merged cards draw under one casing", card.get("passes"), 2)
check("...without losing a block", card.get("blocks"), 3)
check("the lane is drawn at real durations", card.get("seconds"), [5, 7, 9])
check("the facts line counts the shots", card.get("shots"), 3)
check("...and their length", card.get("total"), 21)
check("a cited pool reference pictures its card, and a start frame pictures its own",
      card.get("frames"), [[0, "plate.png"], [2, "open.png"]])
check("a card with a cover collects no block pictures", card.get("framesWithCover"), 0)

check("a finished render becomes a cover path the thumb route takes",
      (report.get("cover") or {}).get("path"), "minimax/renders/H3_00021_.mp4 [output]")
# The field the first cut of this left out, and the whole of why covers were black.
check("...marked as a clip, so it is served by the thumb route and not by /view",
      (report.get("cover") or {}).get("kind"), "video")
check("...in the picker's own row shape, so api.stillUrl needs no adapter",
      sorted((report.get("cover") or {}).keys()), ["kind", "mtime", "path"])
check("a pre-stage still is marked an image, which /view can serve",
      (report.get("coverStill") or {}).get("kind"), "image")
check("...and nothing becomes no cover", report.get("coverEmpty"), None)
check("...as does a stage that has run but saved nothing", report.get("coverNoResult"), None)

# ---- storage ----------------------------------------------------------------

storage = report.get("storage", {})
check("a saved preset keeps its trimmed name", storage.get("named"), True)
check("...and is listed back", storage.get("listed"), True)
check("the index carries the whole card, so the grid draws without a body",
      storage.get("cardInIndex"), True)
check("the body round-trips through storage", storage.get("bodyRoundTrips"), True)
check("starring writes through", storage.get("starred"), True)
check("deleting removes it", storage.get("deleted"), True)

builtin = report.get("builtin", {})
check("the shipped starters describe themselves", builtin.get("allDescribed"), True)
check("...under scopes the library knows", builtin.get("scopesKnown"), True)
check("...holding only sections that scope can take", builtin.get("sectionsAllowed"), True)
check("...and naming no file that is only on one machine", builtin.get("namesNoFiles"), True)

passed(f"presets round-trip, cross scopes and draw — {builtin.get('count', 0)} starters ship")
