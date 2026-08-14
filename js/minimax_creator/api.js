// Talking to the server: list the input folder, upload into it, build view URLs.

import { api } from "../../../scripts/api.js";
import { t } from "./i18n.js";

const cache = new Map();   // root -> {at, assets}
const CACHE_MS = 4000;

/** The media listing: `root: "input"` (the default) is the upload folder,
 *  `root: "output"` is finished renders — the picker's gallery tab. */
export async function listAssets({ force = false, root = "input" } = {}) {
  const hit = cache.get(root);
  if (!force && hit && Date.now() - hit.at < CACHE_MS) return hit.assets;
  const response = await api.fetchApi(`/minimax_creator/assets?root=${encodeURIComponent(root)}`);
  if (!response.ok) throw new Error(t("asset listing failed ({status})", { status: response.status }));
  const body = await response.json();
  const assets = body.assets ?? [];
  cache.set(root, { at: Date.now(), assets, truncated: body.truncated === true });
  return assets;
}

/** Whether the last listing of `root` hit the server's cap — the folder holds
 *  more files than came back. Read after listAssets; false before any call. */
export function listingTruncated(root = "input") {
  return cache.get(root)?.truncated === true;
}

export function invalidate() {
  cache.clear();
}

/** Move one file into another subfolder of the root it already lives in — the
 *  picker's drag-onto-a-shelf. Resolves to the file's new path, annotated as it
 *  came in, so a moved render is still addressable as a render.
 *
 *  Which root is not a parameter: `filename` carries its own ` [output]` when
 *  it is a gallery path, and the server reads the root off that rather than
 *  trusting a second field that could disagree with it. */
export async function moveAsset(filename, subfolder) {
  const response = await api.fetchApi("/minimax_creator/move", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, subfolder }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || t("move failed ({status})", { status: response.status }));
  invalidate();
  return body.path;
}

/** Delete one file, from whichever of the two folders it names. Organize
 *  mode's other action, and the only irreversible one in the picker. */
export async function deleteAsset(filename) {
  const response = await api.fetchApi("/minimax_creator/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || t("delete failed ({status})", { status: response.status }));
  invalidate();
}

// ---- picker preferences -----------------------------------------------------
//
// Favorites and hand-made shelves. Stored per ComfyUI user via the userdata
// API, so they follow the user across browsers; localStorage is the fallback
// for frontends without it. One object: {favorites: [path], folders: [name],
// renderFolders: [name]}.
//
// Two folder lists because the picker browses two folders — `folders` is the
// input one and keeps its name so prefs written before the gallery could be
// organized load unchanged. Favorites need no such split: a gallery path
// carries its ` [output]` annotation, so the two roots cannot collide.

const PREFS_FILE = "minimax_creator.picker.json";
const PREFS_KEY = "mmc-picker-prefs";
let prefsCache = null;

const names = (value) => (Array.isArray(value) ? value.filter((p) => typeof p === "string") : []);

function normalizePrefs(raw) {
  return {
    favorites: names(raw?.favorites),
    folders: names(raw?.folders),
    renderFolders: names(raw?.renderFolders),
  };
}

export async function loadPickerPrefs() {
  if (prefsCache) return prefsCache;
  let raw = null;
  try {
    const response = await api.getUserData(PREFS_FILE);
    if (response.status === 200) raw = await response.json();
  } catch {
    try { raw = JSON.parse(localStorage.getItem(PREFS_KEY) ?? "null"); } catch { /* fresh */ }
  }
  prefsCache = normalizePrefs(raw);
  return prefsCache;
}

export function savePickerPrefs(prefs) {
  prefsCache = normalizePrefs(prefs);
  const body = JSON.stringify(prefsCache);
  try { localStorage.setItem(PREFS_KEY, body); } catch { /* quota; userdata still tries */ }
  // Fire and forget: a star should feel instant, and losing one write is
  // recoverable in a way a blocked click is not.
  try { api.storeUserData(PREFS_FILE, prefsCache, { stringify: true }); } catch { /* offline */ }
}

// ---- settings ---------------------------------------------------------------
//
// Not the userdata API the picker prefs above go through, and the difference
// matters: these are read by the save node while a prompt executes, which has no
// request behind it and so no ComfyUI user. `settings.py` owns the one file both
// ends read, and these two routes are the only way in from here.

/** Every setting, with the keys this build does not know about dropped. */
export async function loadSettings() {
  const response = await api.fetchApi("/minimax_creator/settings");
  if (!response.ok) throw new Error(t("settings failed ({status})", { status: response.status }));
  return (await response.json()).settings ?? {};
}

/** Store some settings and resolve to the whole stored object — what the server
 *  actually wrote, which is what the page then shows. */
export async function saveSettings(patch) {
  const response = await api.fetchApi("/minimax_creator/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || t("settings failed ({status})", { status: response.status }));
  return body.settings ?? {};
}

// The settings page re-fetches on every opening — the server is the only copy
// it trusts. The node bodies cannot do that: the sampler row is drawn
// synchronously on every render, and some of what it draws (the shift pills'
// visibility) is a setting. So this holds the last answer the server gave —
// primed once when the first body mounts, kept current by the settings page
// writing every reply through `noteSettings`. Until the first answer lands the
// fallbacks are in force, which are the server's own defaults.
let uiSettings = null;
let uiSettingsPrimed = null;

export function uiSetting(key, fallback) {
  return uiSettings && key in uiSettings ? uiSettings[key] : fallback;
}

/** The settings page's replies come through here, so the cache is never older
 *  than the last thing the page showed. */
export function noteSettings(settings) {
  uiSettings = settings;
}

/** Fetch the settings once, ever; `onReady` fires when the cache holds them —
 *  immediately, after the first caller's fetch has already landed. */
export function primeSettings(onReady) {
  uiSettingsPrimed = uiSettingsPrimed ?? loadSettings().then(noteSettings).catch(() => {});
  if (onReady) uiSettingsPrimed.then(onReady);
}

let modelsAt = 0;
let modelsCache = null;
let modelsInFlight = null;

/**
 * What the weights control can offer: `{files: {field: [name]}, dtypes,
 * preview_override}`.
 *
 * Every node body asks for this the moment it is built, and a graph can hold a
 * dozen of them, so concurrent callers share one request rather than each
 * walking the model folders. Cached longer than the asset listing: models are
 * downloaded occasionally where input files arrive constantly, and the answer is
 * behind a control you have to open before it matters.
 */
export async function listModels({ force = false } = {}) {
  if (!force && modelsCache && Date.now() - modelsAt < 60000) return modelsCache;
  if (!force && modelsInFlight) return modelsInFlight;
  modelsInFlight = (async () => {
    try {
      const response = await api.fetchApi("/minimax_creator/models");
      if (!response.ok) throw new Error(t("model listing failed ({status})", { status: response.status }));
      modelsCache = await response.json();
      modelsAt = Date.now();
      return modelsCache;
    } finally {
      modelsInFlight = null;
    }
  })();
  return modelsInFlight;
}

/** Core's /view, pointed at output rather than input — how a finished render is
 *  played back in the node body. Takes a `SavedResult` verbatim, which is what
 *  the `executed` message carries. */
export function outputUrl({ filename, subfolder = "", type = "output" }) {
  return api.apiURL(`/view?${new URLSearchParams({ filename, subfolder, type })}`);
}

// Keyed by folder: switching between two folders and back is a normal thing to
// do while hunting for a LoRA, and re-walking a few thousand files for it is not.
const loraCache = new Map();   // folder -> {at, body}

/**
 * One folder of models/loras, each row carrying whatever the sidecars beside it
 * know — CiviMeta, Lora Manager, `.civitai.info`, A1111, or nothing but a
 * preview image. `folder` is a relative path, "" for everything; the reply also
 * carries the folder list, so the manager never has to ask for it separately.
 *
 * `force` is the Rescan button, and it clears the server's caches as well as
 * this one: the server holds a directory listing briefly and a row for as long
 * as nothing beside the file changes, neither of which notices a sidecar edited
 * in place. A button that says "look again" has to reach that far.
 *
 * @returns {Promise<{loras: object[], folders: {path: string, count: number}[],
 *                    folder: string, matched: number, truncated: boolean}>}
 */
export async function listLoras({ folder = "", force = false } = {}) {
  const hit = loraCache.get(folder);
  if (!force && hit && Date.now() - hit.at < CACHE_MS) return hit.body;
  const query = new URLSearchParams({ folder });
  if (force) query.set("refresh", "1");
  const response = await api.fetchApi(`/minimax_creator/loras?${query}`);
  if (!response.ok) throw new Error(t("LoRA listing failed ({status})", { status: response.status }));
  const body = await response.json();
  if (force) loraCache.clear();
  loraCache.set(folder, { at: Date.now(), body });
  return body;
}

/** The card's image or clip, from wherever the server found one — a sidecar's
 *  gallery, a `.preview.png` beside the file, or a thumbnail embedded in the
 *  safetensors header. 404s into the card's fallback when there is nothing. */
export function loraPreviewUrl(name) {
  return api.apiURL(`/minimax_creator/lora_preview?name=${encodeURIComponent(name)}`);
}

const detailCache = new Map();   // name -> {at, detail}

/**
 * Everything the detail sheet shows for one LoRA: the merged sidecar record
 * with its showcase and generation recipes, and the safetensors header either
 * way. Cached briefly — closing and reopening the same sheet is a normal way
 * to read, and nothing in it changes at that cadence.
 */
export async function loraDetail(name) {
  const hit = detailCache.get(name);
  if (hit && Date.now() - hit.at < 60000) return hit.detail;
  const response = await api.fetchApi(`/minimax_creator/lora_detail?name=${encodeURIComponent(name)}`);
  if (!response.ok) throw new Error(t("detail failed ({status})", { status: response.status }));
  const detail = await response.json();
  detailCache.set(name, { at: Date.now(), detail });
  return detail;
}

/** One showcase file by its index in the detail's list; `thumb` asks for the
 *  filmstrip-sized WebP, which falls back to the media file server-side. */
export function loraShowcaseUrl(name, item, { thumb = false } = {}) {
  const params = new URLSearchParams({ name, item: String(item) });
  if (thumb) params.set("thumb", "1");
  return api.apiURL(`/minimax_creator/lora_showcase?${params}`);
}

const PROBES = new Map();   // path -> Promise<{hasAudio, duration, width, height}>

/**
 * What the container header says: `{hasAudio: true|false|null, duration}`, both
 * null when the question could not be answered.
 *
 * `hasAudio` decides whether a reference video is attached with its sound on,
 * and it has to be a server question: `mozHasAudio` is Firefox-only and
 * `audioTracks` is not in Chrome, so there is no portable way to ask the media
 * element. `duration` is the segment editor's fallback for when the browser
 * cannot decode the clip itself. `width`/`height` are the picture's own size,
 * which a clip card stores so the timeline's aspect can come off the footage
 * without the backend opening the file.
 */
export function probe(path) {
  if (!PROBES.has(path)) PROBES.set(path, ask(path));
  return PROBES.get(path);
}

/** Just the soundtrack question, for callers that want nothing else. */
export async function probeAudio(path) {
  return (await probe(path)).hasAudio;
}

async function ask(path) {
  try {
    const response = await api.fetchApi(`/minimax_creator/probe?filename=${encodeURIComponent(path)}`);
    const body = await response.json();
    return {
      hasAudio: typeof body.has_audio === "boolean" ? body.has_audio : null,
      duration: Number.isFinite(body.duration) ? body.duration : null,
      width: Number.isFinite(body.width) ? body.width : null,
      height: Number.isFinite(body.height) ? body.height : null,
    };
  } catch {
    return { hasAudio: null, duration: null, width: null, height: null };
  }
}

/**
 * Core's /view, the same URL LoadImage previews use.
 *
 * Takes the input-relative path ("3d/foo.png"), not an asset row: only the path
 * survives into creator_data, so a reloaded workflow has nothing else to go on.
 */
export function viewUrl(path, { preview = false } = {}) {
  // A gallery path carries ComfyUI's folder annotation ("clip.mp4 [output]").
  // The servers that take a filename parse it themselves; core's /view takes
  // the folder as a parameter instead, so it is split off here.
  const annotated = /^(.*) \[(input|output|temp)\]$/.exec(String(path));
  const clean = annotated ? annotated[1] : String(path);
  const at = clean.lastIndexOf("/");
  const params = new URLSearchParams({
    filename: at < 0 ? clean : clean.slice(at + 1),
    subfolder: at < 0 ? "" : clean.slice(0, at),
    type: annotated ? annotated[2] : "input",
  });
  // Core re-encodes to webp when asked. It does not downscale, but a 4000px
  // PNG served as q70 webp is a fraction of the bytes, and a picker showing
  // thirty of them at 140px has no use for the originals.
  if (preview) params.set("preview", "webp;70");
  return api.apiURL(`/view?${params}`);
}

/**
 * A server-decoded still of one clip.
 *
 * The grid used to hang a <video preload="metadata"> in every cell and let the
 * browser seek for a frame. That is one media download per cell through a
 * six-connection budget, megabytes each to paint 140 px, and it needs the
 * browser to have an H.264 decoder at all — which a distro Chromium often does
 * not. This is a few KB of JPEG instead.
 *
 * `version` is the asset's mtime, which is what makes the URL safe to cache
 * forever: re-uploading the file changes the URL rather than staling the image.
 */
export function thumbUrl(path, version) {
  const params = new URLSearchParams({ filename: path });
  if (version) params.set("v", String(version));
  return api.apiURL(`/minimax_creator/thumb?${params}`);
}

/**
 * Waveform peaks for the segment editor, normalised to 0..1, or null when there
 * is nothing to draw. Decoded server-side and cached there by mtime.
 */
export async function fetchPeaks(path) {
  try {
    const response = await api.fetchApi(`/minimax_creator/peaks?filename=${encodeURIComponent(path)}`);
    if (!response.ok) return null;
    const body = await response.json();
    return Array.isArray(body.peaks) ? Float32Array.from(body.peaks) : null;
  } catch {
    return null;
  }
}

/**
 * Upload into the input folder. Core's /upload/image is what LoadVideo and
 * LoadAudio post to as well, despite the name — there is no separate endpoint.
 */
export async function upload(file, subfolder = "") {
  const form = new FormData();
  form.append("image", file);
  if (subfolder) form.append("subfolder", subfolder);
  const response = await api.fetchApi("/upload/image", { method: "POST", body: form });
  if (!response.ok) throw new Error(t("upload failed ({status})", { status: response.status }));
  const body = await response.json();
  invalidate();
  return {
    path: body.subfolder ? `${body.subfolder}/${body.name}` : body.name,
    name: body.name,
    subfolder: body.subfolder || "",
  };
}
