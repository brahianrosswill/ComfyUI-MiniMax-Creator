// Pre-stage and the frame grab.
// No backticks or ${} anywhere in the CSS: each chunk is one template literal.
export const css = `
/* ---- pre-stage --------------------------------------------------------------
   The image node wears the Creator's clothes: same tokens, same pills, same
   chip vocabulary. Only what it does not share is styled here. */

/* A plain textarea, not the contenteditable PromptBox: an image prompt has no
   @-handles to chip. Dressed exactly like the timeline's prompt box. */
.mmc-prestage-prompt {
  width: 100%; box-sizing: border-box; min-height: 96px; resize: vertical;
  background: var(--mmc-surface); border: 1px solid var(--mmc-line); border-radius: 14px;
  color: var(--mmc-text); font-family: inherit; font-size: 14px; line-height: 1.5;
  padding: 14px 16px; outline: none;
}
/* In the window it has the room, so it takes a paragraph's worth of it — the
   window scrolls around it rather than the box stretching to whatever is in it,
   which is the same bargain the Creator's box takes there. */
.mmc-editor-sheet-body .mmc-prestage-prompt { min-height: 40vh; }
.mmc-prestage-prompt:focus { border-color: rgba(255,255,255,.2); }
.mmc-prestage-prompt::placeholder { color: var(--mmc-off); }

/* The spawn pill. On, it wears the accent the continue pill wears — the
   pre-stage is part of this shot now, which is a stronger statement than the
   accelerators' blue "not native". */
.mmc-prestage-toggle.on { border-color: rgba(240,166,60,.5); color: var(--mmc-accent); }
.mmc-prestage-toggle.on:hover:not(:disabled) { border-color: rgba(240,166,60,.8); }

/* The left-hand satellite anchors on its right edge (satellite.js sets the
   transform); nothing else about the card changes side. */
.mmc-satellite-left { transform-origin: 100% 0; }

/* The hand-off chips on a finished still — real buttons in the readout row,
   dressed like the gallery chip so the overlay stays one vocabulary. The
   readout swallows the pointer (see above), so like the gallery chip these
   have to opt back in or they are pictures of buttons. */
.mmc-stage-send {
  pointer-events: auto;
  background: rgba(0,0,0,.55); border: 1px solid #4a4a4a; border-radius: 999px;
  padding: 3px 10px; cursor: pointer; font-family: inherit; font-size: 12px;
  color: #ededed;
}
.mmc-stage-send:hover { border-color: var(--mmc-accent); color: var(--mmc-accent); }

/* ---- the frame grab ---------------------------------------------------------
   The trim editor's scrubbing with a different ending; dressed like it too. */
.mmc-grab-card {
  display: flex; flex-direction: column; gap: 14px;
  width: min(720px, 92vw); padding: 20px 24px;
  background: var(--mmc-bg); border: 1px solid var(--mmc-line); border-radius: 18px;
  box-shadow: 0 24px 64px rgba(0,0,0,.55);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
  color: var(--mmc-text); font-size: 13px;
}
.mmc-grab-title { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.mmc-grab-title svg { stroke: currentColor; fill: none; stroke-width: 1.6; }
.mmc-grab-stage {
  width: 100%; max-height: 46vh; object-fit: contain;
  background: #000; border-radius: 12px;
}
.mmc-grab-row { display: flex; align-items: center; gap: 10px; }
.mmc-grab-scrub { flex: 1; }
.mmc-grab-time { min-width: 64px; text-align: right; color: var(--mmc-dim); font-variant-numeric: tabular-nums; }
.mmc-grab-actions { display: flex; justify-content: flex-end; gap: 12px; }
.mmc-grab-actions .mmc-btn {
  padding: 8px 18px; border-radius: 999px; cursor: pointer; font-family: inherit;
  font-size: 13px; background: var(--mmc-surface-2); color: var(--mmc-text);
  border: 1px solid var(--mmc-line);
}
.mmc-grab-actions .mmc-btn:hover:not(:disabled) { border-color: rgba(255,255,255,.25); }
.mmc-grab-actions .mmc-btn-primary { background: var(--mmc-accent); color: #141414; border-color: transparent; }
.mmc-grab-actions .mmc-btn:disabled { opacity: .5; cursor: progress; }
`;
