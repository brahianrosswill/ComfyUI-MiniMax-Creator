// The preset library: the window you save a setup into and get it back from.
//
// It is the picker's window with a different grid in it — scope tabs where the
// kind tabs are, shelves where the shelves are, search where the search is —
// because a user who has opened the asset picker once has already learned this
// one. What differs is the cell: a preset's content is structure rather than a
// picture, so a 140px square would waste the middle of every one, and the card is
// wide with a line of prose and a line of numbers under its hero.
//
// **The hero is the strip.** The node face already draws the piece as blocks at
// their real relative lengths, merged shots closed up under one casing; that
// drawing *is* the shape of the piece and it is generated from data the preset
// already holds. Where the preset carries a cover — the render it was saved from
// — the cover takes the band and the lane is redrawn as a ruler across its foot,
// so the shape stays legible without competing with the picture.
//
// Nothing here stores an image. A cover is a filename in the output folder and a
// block's picture is a filename the preset had to hold anyway; both are served by
// routes that shipped long before presets did.

import { el, icon, mountOverlay } from "./dom.js";
import { t } from "./i18n.js";
import { stillUrl } from "./api.js";
import { openPicker } from "./picker.js";
import { BUILTIN } from "./presets/builtin.js";
import * as P from "./presets.js";

const SHELF_ALL = "all";
const SHELF_FAV = "fav";

/**
 * Open the library.
 *
 * @param {object} options
 * @param {object} options.target  what a preset can be applied to:
 *   `{scope, label, capture(), apply(body, keys, fromScope), arch()}`. Null opens
 *   the library read-only, which is what the node context menu does when there is
 *   nothing sensible to apply to.
 * @returns {Promise<void>}
 */
export function openPresetLibrary(options) {
  return new Promise((resolve) => new PresetLibrary(options, resolve).mount());
}

/** mm:ss, which is how a length is read off a strip. */
function clock(seconds) {
  const whole = Math.max(0, Math.round(seconds || 0));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

class PresetLibrary {
  constructor({ target = null }, resolve) {
    this.target = target;
    this.resolve = resolve;
    // Opens on the scope the node can actually take, because that is what you
    // came for. The tabs are still there to browse the rest.
    this.scope = target?.scope ?? "piece";
    this.query = "";
    this.shelf = SHELF_ALL;
    this.rows = [];
    this.selected = null;      // the row the inspector is showing
    this.body = null;          // its sections, once fetched
    this.keys = new Set();     // which of them are ticked
    this.problem = null;
    this.busy = false;
    // Which preset's Delete is armed, if any — the picker's two-press confirm.
    this.armed = null;
  }

  mount() {
    this.grid = el("div", { class: "mmc-preset-grid" });
    this.inspector = el("aside", { class: "mmc-preset-insp" });
    this.problemLine = el("div", { class: "mmc-preset-problem", style: { display: "none" } });

    this.search = el("input", {
      class: "mmc-search",
      type: "search",
      placeholder: t("Search presets…"),
      oninput: (event) => { this.query = event.target.value.toLowerCase(); this.renderGrid(); },
      onkeydown: (event) => event.stopPropagation(),
    });

    this.tabs = P.SCOPES.map((scope) => el("button", {
      class: "mmc-tab",
      "aria-selected": scope === this.scope,
      text: t(P.SCOPE_LABEL[scope]),
      onclick: () => this.selectScope(scope),
    }));

    this.shelfRow = el("div", { class: "mmc-shelves" });

    this.modal = el("div", { class: "mmc-modal" }, [
      el("div", { class: "mmc-modal-head" }, [
        ...this.tabs,
        el("button", { class: "mmc-close", text: "✕", title: t("Close"), onclick: () => this.close() }),
      ]),
      el("div", { class: "mmc-modal-bar" }, [
        this.search,
        el("button", {
          class: "mmc-organize",
          title: t("Read a .json of presets exported from another machine"),
          onclick: () => this.importFile(),
        }, [icon("folder", 14), el("span", { text: t("Import") })]),
        // Absent rather than disabled where there is nothing to save: the
        // library opened from a context menu has no node behind it.
        ...(this.target ? [el("button", {
          class: "mmc-upload",
          text: t("+  Save current setup"),
          onclick: () => this.saveCurrent(),
        })] : []),
      ]),
      this.shelfRow,
      this.problemLine,
      el("div", { class: "mmc-preset-split" }, [this.grid, this.inspector]),
    ]);
    this.modal.style.position = "relative";

    this.overlay = el("div", {
      class: "mmc-overlay",
      onpointerdown: (event) => { if (event.target === this.overlay) this.close(); },
    }, [this.modal]);

    this.unmount = mountOverlay(this.overlay, () => this.close());
    this.renderInspector();
    this.load();
  }

  async load() {
    try {
      const stored = await P.listPresets({ force: true });
      // Builtins last within their scope: a shipped starter is a suggestion and
      // your own work is the library.
      this.rows = [...stored, ...BUILTIN];
    } catch (error) {
      this.rows = [...BUILTIN];
      this.say(t("Could not read the library — {error}", { error: error.message }));
    }
    this.renderShelves();
    this.renderGrid();
  }

  close() {
    this.unmount();
    this.resolve();
  }

  say(problem) {
    this.problem = problem;
    this.problemLine.textContent = problem ?? "";
    this.problemLine.style.display = problem ? "" : "none";
  }

  selectScope(scope) {
    if (scope === this.scope) return;
    this.scope = scope;
    this.selected = null;
    this.body = null;
    // A shelf is a place, not a scope — but "starred" and a hand-made folder
    // both survive the move, so only the selection is dropped.
    for (const [index, tab] of P.SCOPES.entries()) {
      this.tabs[index].setAttribute("aria-selected", String(tab === scope));
    }
    this.renderShelves();
    this.renderGrid();
    this.renderInspector();
  }

  // ---- shelves --------------------------------------------------------------

  folders() {
    return [...new Set(this.rows.filter((row) => row.scope === this.scope && row.folder)
      .map((row) => row.folder))].sort();
  }

  renderShelves() {
    const shelves = [
      [SHELF_ALL, t("All")],
      [SHELF_FAV, t("★ Starred")],
      ...this.folders().map((folder) => [folder, folder]),
    ];
    if (!shelves.some(([key]) => key === this.shelf)) this.shelf = SHELF_ALL;
    this.shelfRow.replaceChildren(...shelves.map(([key, label]) => el("button", {
      class: "mmc-shelf",
      "aria-pressed": key === this.shelf,
      text: label,
      onclick: () => { this.shelf = key; this.renderGrid(); },
    })));
  }

  visible() {
    return this.rows.filter((row) => {
      if (row.scope !== this.scope) return false;
      if (this.shelf === SHELF_FAV && !row.starred) return false;
      if (this.shelf !== SHELF_ALL && this.shelf !== SHELF_FAV && row.folder !== this.shelf) return false;
      if (!this.query) return true;
      return `${row.name} ${row.note ?? ""} ${row.folder ?? ""}`.toLowerCase().includes(this.query);
    });
  }

  // ---- the grid -------------------------------------------------------------

  renderGrid() {
    const rows = this.visible();
    if (!rows.length) {
      this.grid.replaceChildren(el("div", { class: "mmc-preset-empty", text: this.emptyWords() }));
      return;
    }
    this.grid.replaceChildren(...rows.map((row) => this.renderCard(row)));
  }

  emptyWords() {
    if (this.query) return t("Nothing here matches “{query}”.", { query: this.query });
    if (this.shelf === SHELF_FAV) return t("No starred presets yet. The star on a card puts it here.");
    if (this.target?.scope === this.scope) {
      return t("No presets yet. Set this node up the way you want it, then Save current setup.");
    }
    return t("No presets of this kind yet.");
  }

  renderCard(row) {
    // The card and its star are siblings in a wrapper rather than the star being
    // inside the card: a button inside a button is invalid, and the inner one's
    // clicks are the browser's to route however it likes.
    const holder = el("div", { class: "mmc-preset-holder" });
    const card = el("button", {
      class: "mmc-preset-card",
      "aria-selected": this.selected?.id === row.id,
      "data-builtin": row.builtin ? "" : null,
      onclick: () => this.select(row),
    }, [
      this.renderHero(row),
      el("p", { class: "mmc-preset-name", text: row.name }),
      el("p", { class: "mmc-preset-facts", text: this.factsLine(row) }),
      el("div", { class: "mmc-preset-chips" }, [
        ...(row.sections ?? []).map((key) => el("span", {
          class: `mmc-preset-chip mmc-tag-${P.SECTION[key]?.hue ?? 0}`,
          text: t(P.SECTION[key]?.label ?? key).toLowerCase(),
        })),
        ...(row.builtin ? [el("span", { class: "mmc-preset-chip plain", text: t("built-in") })] : []),
      ]),
    ]);
    holder.append(card);
    // Not on a builtin: a shipped starter is the same for everybody and has
    // nowhere to keep a star.
    if (!row.builtin) {
      holder.append(el("button", {
        class: "mmc-preset-star",
        "aria-pressed": row.starred === true,
        title: row.starred ? t("Remove from Starred") : t("Add to Starred"),
        onclick: () => this.toggleStar(row),
      }, [icon("star", 14)]));
    }
    return holder;
  }

  /**
   * The hero, in its three states — see the stylesheet. Each is the fallback of
   * the one before it: a cover, else the pictured lane, else the bare shape.
   */
  renderHero(row) {
    const cover = stillUrl(row.cover);
    const hero = el("div", { class: "mmc-preset-hero", "data-cover": cover ? "" : null });
    if (cover) {
      hero.append(el("img", {
        class: "mmc-preset-cover",
        // A render since deleted is a 404, and the card falls back to the lane
        // underneath rather than showing a broken picture. The same honest
        // fallback a missing block has. Before `src`, because `el` sets props in
        // order and a listener attached after the request is a listener that can
        // miss it.
        onerror: (event) => {
          event.target.remove();
          hero.removeAttribute("data-cover");
        },
        src: cover, alt: "", loading: "lazy",
      }));
    }
    if (row.scope === "prestage") {
      if (!cover) hero.append(this.renderCanvasFigure(row));
      return hero;
    }
    if (row.scope === "shot") {
      if (!cover) hero.append(this.renderSolo(row));
      return hero;
    }
    hero.append(this.renderLane(row, { pictured: !cover }));
    return hero;
  }

  renderLane(row, { pictured }) {
    const runs = row.lane?.runs ?? [];
    const frames = new Map((row.frames ?? []).map((frame) => [frame.at, frame]));
    return el("div", { class: "mmc-preset-lane" }, runs.map((run) => {
      const seconds = run.blocks.reduce((total, block) => total + block.seconds, 0);
      return el("div", {
        class: "mmc-preset-pass",
        // A pass is as wide as it is long, and a block inside it is as wide as
        // its share of the pass — the reading the node's own reel gives.
        style: { flex: String(seconds) },
      }, run.blocks.map((block) => {
        const cell = el("i", {
          class: "mmc-preset-blk",
          "data-clip": block.clip ? "" : null,
          style: { flex: String(block.seconds) },
        });
        const picture = pictured ? stillUrl(frames.get(block.at)) : null;
        if (picture) {
          cell.append(el("img", {
            src: picture, alt: "", loading: "lazy",
            onerror: (event) => event.target.remove(),
          }));
        }
        return cell;
      }));
    }));
  }

  renderSolo(row) {
    const seconds = row.facts?.seconds ?? 0;
    // Against a nominal twenty-second card, so a 12 s shot is visibly longer
    // than a 6 s one without a 90 s one running off the end.
    const share = Math.max(0.14, Math.min(1, seconds / 20));
    const block = el("i", {
      class: "mmc-preset-blk",
      "data-clip": row.facts?.clip ? "" : null,
      style: { width: `${Math.round(share * 100)}%` },
    });
    const picture = stillUrl((row.frames ?? [])[0]);
    if (picture) {
      block.append(el("img", { src: picture, alt: "", loading: "lazy",
                               onerror: (event) => event.target.remove() }));
    }
    return el("div", { class: "mmc-preset-solo" }, [
      block,
      el("em", { text: t("{n} s", { n: +seconds.toFixed(1) }) }),
    ]);
  }

  renderCanvasFigure(row) {
    const [w, h] = String(row.canvas?.aspect ?? row.facts?.aspect ?? "16:9").split(":").map(Number);
    const ratio = w && h ? w / h : 16 / 9;
    const frame = el("span", { style: { width: `${Math.round(84 * ratio)}px` } });
    const picture = stillUrl(row.canvas?.picture);
    if (picture) {
      frame.append(el("img", { src: picture, alt: "", loading: "lazy",
                               onerror: (event) => event.target.remove() }));
    }
    return el("div", { class: "mmc-preset-canvas" }, [frame]);
  }

  factsLine(row) {
    const facts = row.facts ?? {};
    if (row.scope === "prestage") {
      return [facts.arch, facts.aspect, facts.quality].filter(Boolean).join(" · ");
    }
    if (row.scope === "shot") {
      return [
        facts.clip ? t("clip") : t("shot"),
        t("{n} s", { n: +(facts.seconds ?? 0).toFixed(1) }),
        facts.feather ? t("feather {n}", { n: facts.feather }) : null,
        facts.checkpoint && facts.checkpoint !== "auto" ? facts.checkpoint : null,
      ].filter(Boolean).join(" · ");
    }
    const shots = facts.shots ?? 0;
    return [
      t(shots === 1 ? "{count} shot" : "{count} shots", { count: shots }),
      clock(facts.seconds),
      facts.passes && facts.passes !== shots
        ? t("{count} passes", { count: facts.passes }) : null,
      facts.route && facts.route !== "auto" ? facts.route : null,
      facts.aspect,
    ].filter(Boolean).join(" · ");
  }

  // ---- the inspector --------------------------------------------------------

  async select(row) {
    this.selected = row;
    this.body = null;
    // An armed Delete belongs to the preset it was armed on; moving away is
    // changing your mind about it.
    this.armed = null;
    this.say(null);
    this.renderGrid();
    this.renderInspector();
    const body = await P.loadBody(row);
    // A second click while the first was in flight: only paint for the row that
    // is still selected.
    if (this.selected?.id !== row.id) return;
    this.body = body;
    // Everything applicable, ticked. "Everything" is the right default; being
    // able to take part of it is what stops the library going unused the moment
    // you have a prompt worth keeping.
    this.keys = new Set(Object.keys(body ?? {}).filter((key) => this.crossable(key, row).ok));
    this.renderInspector();
  }

  crossable(key, row) {
    if (!this.target) return { ok: false, why: t("Nothing to apply this to — open the library from a node.") };
    return P.crossable(key, row.scope, this.target.scope, {
      arch: row.facts?.arch ?? null,
      targetArch: this.target.arch?.() ?? null,
    });
  }

  renderInspector() {
    const row = this.selected;
    if (!row) {
      this.inspector.replaceChildren(el("div", { class: "mmc-preset-insp-hint", text:
        this.target
          ? t("Pick a preset to see what is in it and choose what to apply.")
          : t("Pick a preset to see what is in it.") }));
      return;
    }
    if (!this.body) {
      this.inspector.replaceChildren(
        el("div", { class: "mmc-preset-insp-title", text: row.name }),
        el("div", { class: "mmc-preset-insp-hint", text: t("Reading…") }));
      return;
    }

    const applicable = [...this.keys].length;
    this.inspector.replaceChildren(
      // A builtin's name is not editable — it is the same for everybody, and
      // "Save as…" from one is how you get a copy that is yours.
      row.builtin
        ? el("div", { class: "mmc-preset-insp-title", text: row.name })
        : el("input", {
            class: "mmc-preset-insp-name",
            value: row.name,
            "aria-label": t("Preset name"),
            onkeydown: (event) => {
              event.stopPropagation();
              if (event.key === "Enter") event.target.blur();
            },
            onchange: (event) => this.rename(row, event.target.value),
          }),
      this.renderMeta(row),
      el("div", { class: "mmc-preset-rows" }, this.renderSectionRows(row)),
      ...(this.target ? [el("button", {
        class: "mmc-preset-apply",
        disabled: !applicable || this.busy,
        text: applicable
          ? t("Apply to {label} ({count})", { label: this.target.label, count: applicable })
          : t("Nothing here fits this node"),
        onclick: () => this.apply(row),
      })] : []),
      el("div", { class: "mmc-preset-insp-acts" }, [
        el("button", {
          class: "mmc-preset-danger",
          text: t("Export"),
          onclick: () => P.exportPresets([row], [this.body]),
        }),
        // Two presses, the picker's own deal for the same irreversible verb —
        // rather than a browser confirm() this page cannot style or place.
        ...(row.builtin ? [] : [el("button", {
          class: `mmc-preset-danger${this.armed === row.id ? " armed" : ""}`,
          text: this.armed === row.id ? t("Really delete?") : t("Delete"),
          onclick: () => {
            if (this.armed === row.id) { this.remove(row); return; }
            this.armed = row.id;
            this.renderInspector();
          },
        })]),
      ]),
    );
  }

  renderMeta(row) {
    const meta = el("p", { class: "mmc-preset-insp-meta" });
    const when = new Date(row.updated ?? row.created ?? Date.now());
    meta.append(el("span", { text: t("Updated {date}", { date: when.toLocaleDateString() }) }));
    if (row.builtin) return meta;
    meta.append(el("br"));
    meta.append(el("span", {
      // The bare filename: the folder is the output prefix's business and the
      // ` [output]` annotation is machinery, not something to read.
      text: row.cover
        ? t("Cover: {name} · ", { name: row.cover.path.replace(/ \[\w+\]$/, "").split("/").pop() })
        : t("No cover · "),
    }));
    meta.append(el("button", {
      text: row.cover ? t("Change") : t("Set"),
      onclick: () => this.pickCover(row),
    }));
    if (row.cover) {
      meta.append(el("span", { text: " · " }));
      meta.append(el("button", { text: t("Clear"), onclick: () => this.setCover(row, null) }));
    }
    return meta;
  }

  renderSectionRows(row) {
    return (row.sections ?? []).map((key) => {
      const section = P.SECTION[key];
      const cross = this.crossable(key, row);
      const on = this.keys.has(key);
      return el("button", {
        class: "mmc-preset-row",
        "aria-checked": on,
        disabled: !cross.ok,
        // A section that cannot cross is shown and disabled with the reason on
        // it, never hidden: a missing row is a bug the user reports.
        title: cross.ok ? "" : t(cross.why),
        onclick: () => {
          if (on) this.keys.delete(key); else this.keys.add(key);
          this.renderInspector();
        },
      }, [
        el("span", { class: "mmc-preset-box" }),
        el("span", { class: "mmc-preset-text" }, [
          el("b", { text: t(section?.label ?? key) }),
          el("span", { text: cross.ok ? this.describeSection(key) : t(cross.why) }),
        ]),
      ]);
    });
  }

  /** What this section actually holds, read off the body — so the row says "3
   *  LoRAs" rather than repeating the same sentence about what a LoRA is. */
  describeSection(key) {
    const body = this.body ?? {};
    const section = P.SECTION[key];
    switch (key) {
      case "look": {
        const look = body.look ?? {};
        return [look.aspect, look.short_edge ? t("{n} short edge", { n: look.short_edge }) : null,
                look.upscale === "two_pass" ? t("two-pass") : null].filter(Boolean).join(" · ");
      }
      case "weights": {
        const weights = body.weights ?? {};
        if (weights.arch) return t("{arch}, its own files", { arch: weights.arch });
        const files = Object.keys(weights).filter((field) => typeof weights[field] === "string"
          && field !== "dtype" && field !== "route").length;
        return [t(files === 1 ? "{count} file" : "{count} files", { count: files }),
                weights.route && weights.route !== "auto" ? t("routed {route}", { route: weights.route }) : null]
          .filter(Boolean).join(" · ");
      }
      case "speed": {
        const row = body.speed?.row ?? {};
        return [row.steps ? t("{n} steps", { n: row.steps }) : null,
                row.sampler_name, row.scheduler,
                body.speed?.turbo?.on ? t("turbo") : null].filter(Boolean).join(" · ");
      }
      case "prompt": {
        const text = (body.prompt?.prompt ?? "").trim();
        return text ? text.slice(0, 90) : t("empty");
      }
      case "loras": {
        const count = (body.loras ?? []).length;
        return t(count === 1 ? "{count} LoRA" : "{count} LoRAs", { count });
      }
      case "refs": {
        const refs = body.refs;
        const count = Array.isArray(refs)
          ? refs.length
          : (refs?.refs?.length ?? 0) + (refs?.init ? 1 : 0);
        return t(count === 1 ? "{count} file" : "{count} files", { count });
      }
      case "strip": {
        const segments = body.strip?.segments ?? [];
        const seams = segments.filter((segment) => segment.continue).length;
        return [t(segments.length === 1 ? "{count} card" : "{count} cards", { count: segments.length }),
                seams ? t("{count} continuations", { count: seams }) : null].filter(Boolean).join(" · ");
      }
      case "shot": {
        const shot = body.shot ?? {};
        return [t("{n} s", { n: shot.duration_s ?? 0 }),
                shot.continue ? t("continues") : t("hard cut"),
                shot.merge ? t("merged") : null].filter(Boolean).join(" · ");
      }
      default:
        return t(section?.hint ?? "");
    }
  }

  // ---- the verbs ------------------------------------------------------------

  async apply(row) {
    if (this.busy) return;
    this.busy = true;
    try {
      this.target.apply(this.body, [...this.keys], row.scope);
      this.close();
    } catch (error) {
      this.busy = false;
      this.say(t("Could not apply it — {error}", { error: error.message }));
      this.renderInspector();
    }
  }

  /**
   * Save what the node is set to right now.
   *
   * Saved first and named after, rather than asked for a name up front: the
   * preset is the work, the name is a label on it, and there is nowhere better
   * to type one than the field the inspector already has. So it lands under the
   * first line of its own prompt, opens selected, and the name field takes focus
   * with the text selected — type over it or leave it.
   *
   * No `prompt()` and no `confirm()` anywhere in here. Nothing else in this pack
   * uses a browser dialog, and a modal the page cannot style is exactly the kind
   * of seam a library is supposed to hide.
   */
  async saveCurrent() {
    const captured = this.target.capture();
    try {
      const row = await P.savePreset({
        name: captured.defaultName || t("Untitled preset"),
        scope: this.target.scope,
        data: captured.data,
        cover: captured.cover ?? null,
      });
      this.rows = [row, ...this.rows];
      this.scope = row.scope;
      for (const [index, scope] of P.SCOPES.entries()) {
        this.tabs[index].setAttribute("aria-selected", String(scope === this.scope));
      }
      this.say(null);
      this.renderShelves();
      this.renderGrid();
      await this.select(row);
      // The name is the one thing still to decide, so the caret is already in it.
      const field = this.inspector.querySelector?.(".mmc-preset-insp-name");
      field?.focus();
      field?.select?.();
    } catch (error) {
      this.say(error.message);
    }
  }

  async toggleStar(row) {
    try {
      const updated = await P.updatePreset(row.id, { starred: !row.starred, updated: row.updated });
      this.rows = this.rows.map((entry) => (entry.id === row.id ? updated : entry));
      if (this.selected?.id === row.id) this.selected = updated;
      this.renderShelves();
      this.renderGrid();
    } catch (error) {
      this.say(error.message);
    }
  }

  async rename(row, name) {
    const trimmed = name.trim();
    if (!trimmed || trimmed === row.name) return;
    try {
      const updated = await P.updatePreset(row.id, { name: trimmed });
      this.rows = this.rows.map((entry) => (entry.id === row.id ? updated : entry));
      this.selected = updated;
      this.renderGrid();
    } catch (error) {
      this.say(error.message);
    }
  }

  /** Set the cover from the gallery — the same window the rail's Gallery tool
   *  opens, because picking a render is exactly what it is for. */
  async pickCover(row) {
    const chosen = await openPicker({
      kinds: ["renders"],
      kind: "renders",
      capacity: () => ({ used: 0, max: 1, filesLeft: 1 }),
    });
    if (!chosen?.length) return;
    const picked = chosen[0];
    // The picker's row *is* the shape a cover is stored in — see
    // `coverFromResult`. Copied field for field rather than rebuilt.
    this.setCover(row, { path: picked.path, kind: picked.kind, mtime: picked.mtime });
  }

  async setCover(row, cover) {
    try {
      // The frames go with it: with a cover the lane is a ruler and draws no
      // pictures, and clearing one has to put them back.
      const updated = await P.updatePreset(row.id, {
        cover,
        ...P.describe(this.body ?? {}, row.scope, { cover }),
      });
      this.rows = this.rows.map((entry) => (entry.id === row.id ? updated : entry));
      this.selected = updated;
      this.renderGrid();
      this.renderInspector();
    } catch (error) {
      this.say(error.message);
    }
  }

  async remove(row) {
    try {
      await P.deletePreset(row.id);
      this.armed = null;
      this.rows = this.rows.filter((entry) => entry.id !== row.id);
      this.selected = null;
      this.body = null;
      this.renderShelves();
      this.renderGrid();
      this.renderInspector();
    } catch (error) {
      this.say(error.message);
    }
  }

  importFile() {
    const input = el("input", { type: "file", accept: ".json,application/json",
                                style: { display: "none" } });
    input.addEventListener("change", async () => {
      const file = input.files?.[0];
      input.remove();
      if (!file) return;
      try {
        const saved = await P.importPresets(file);
        this.say(null);
        await this.load();
        if (saved.length) this.select(saved[0]);
      } catch (error) {
        this.say(t("Could not import — {error}", { error: error.message }));
      }
    });
    document.body.appendChild(input);
    input.click();
  }
}
