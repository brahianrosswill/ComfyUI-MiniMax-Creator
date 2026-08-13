"""A clip made of several shots, in one of two ways.

**Chained** is what the graph machinery below is for: one generation per segment,
concatenated, with segment N able to start from segment N-1's decoded last frame.
It buys length — there is no bound on the finished clip — at the cost of a real
seam at every join.

**One pass** is the other reading of the same timeline. H3's own prompt format is
already a shot list with cut times (`[Shot 2] At 00:05.000, the camera cuts to
...`), so the segments can be compiled into a single multi-shot description and
generated in one go. Nothing is decoded and re-encoded mid-clip, which is what
removes the seam entirely: continuity, sound and colour carry because they were
never broken. `compile.single_payload` does the whole of it — the timeline
becomes one ordinary request and everything downstream is unchanged. What it
costs is anything one pass can only have one of: one mode, one checkpoint, one
LoRA stack, one seed, and no per-segment continuation to switch.

The rest of this module is the chained path.

The Creator node hands out conditioning and lets the graph own the sampler. A
timeline cannot: segment 2 starts from segment 1's *decoded* last frame, so the
chain has a data dependency that only exists downstream of sampling. Returning
conditioning N times would not express it, and feeding the result back into the
node's own input would be a cycle the executor refuses to run.

So this node builds the graph instead of being a node in it. `execute` compiles
the timeline, emits one `segment -> KSampler -> decode` chain per segment with
each chain's last frame wired into the next, and returns that subgraph through
ComfyUI's `expand` mechanism. The "feed the result back" is a genuine forward
edge in a generated graph, not a loop.

Two consequences worth knowing before reading further:

- **This node owns the sampler.** It has to, because it is the thing writing the
  KSampler into the graph. That is the price of chaining and the reason this is
  a second node rather than a mode of the first — the Creator's contract, where
  you wire your own sampler, is still the better one for a single clip.
- **Editing a segment only re-runs that segment and the ones after it.** What
  buys that is easy to lose: each segment node is handed its own payload rather
  than the whole timeline, so a payload changes only when its own segment does.
  Hand a segment the whole blob and editing the last shot re-generates all of
  them. The loaders `models.emit_links` writes are ordinary nodes keyed on their
  filenames, so they cache the same way and are built once for the whole chain.
"""

import json

from comfy_api.latest import io

from . import (accel, canvas, compile as compiler, encode as encoder, lora,
               media, models, mux, outputs, payload as payload_repair, render,
               settings)

# The reel's own socket type. A list of `{"images", "audio"}` in play order —
# see `MiniMaxH3Reel` for why the passes travel as a list of references rather
# than as one concatenated clip.
REEL_TYPE = "MMC_REEL"

DEFAULT_DATA = json.dumps({
    "version": 2,
    "render": "chained",
    "prompt": "",
    "soundscape": "",
    "music": "",
    "aspect": "16:9",
    "short_edge": 768,
    "loras": [],
    # The piece's own reference pool — a character sheet, a location plate —
    # cited by handle from any segment's text and injected into exactly the
    # segments that cite it. See `compile.timeline_pool`.
    "assets": [],
    # Where the finished clip lands under output/. See `outputs`.
    "output_prefix": outputs.VIDEO_PREFIX,
    # Which files to load. Empty here rather than guessed: a fresh node has no
    # idea what is on this machine, and the UI fills it from the listing route.
    "models": {},
    "segments": [
        {"prompt": "", "assets": [], "loras": [], "duration_s": 6, "checkpoint": "auto"},
    ],
}, indent=2)


def _parse(timeline_data):
    try:
        return json.loads(timeline_data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"timeline_data is not valid JSON: {exc}") from exc


def _announce(unique_id, progress):
    """Broadcast which segment is being built, keyed to the emitting node.

    `mmc_segment` carries the expanded node's own id — the Timeline's plus a
    GraphBuilder prefix — which `stage.js` prefix-matches exactly as it does
    for the sampler's preview frames. Sent through the running PromptServer;
    a graph executed without one (the test harness) has nobody to tell.
    """
    from server import PromptServer

    server = getattr(PromptServer, "instance", None)
    if server is not None:
        server.send_sync("mmc_segment", {"node": unique_id, **progress})


def _stamps(data):
    """Mtimes of every file any segment names, for `fingerprint_inputs`."""
    import os

    out = []

    def stamp(path_of, item, key):
        try:
            out.append(os.path.getmtime(path_of(item.get(key, ""))))
        except Exception:
            out.append(None)

    # The timeline's own LoRAs are patched onto every segment, so a replaced file
    # has to invalidate the node just as a segment's own would. The reference
    # pool is the same story on the asset side: a cited pool file rides into
    # segments, so replacing it has to re-render them.
    for entry in data.get("loras", []) or []:
        stamp(lora.resolve, entry, "name")
    for asset in data.get("assets", []) or []:
        stamp(media.resolve, asset, "filename")
    for segment in data.get("segments", []):
        if not isinstance(segment, dict):
            continue
        # A supplied clip's own file. Without this, replacing the footage under
        # a card that has not otherwise changed would be a cache hit and the
        # render would keep playing the clip that is no longer there.
        if segment.get("filename"):
            stamp(media.resolve, segment, "filename")
        for asset in segment.get("assets", []) or []:
            stamp(media.resolve, asset, "filename")
        for entry in segment.get("loras", []) or []:
            stamp(lora.resolve, entry, "name")
    return tuple(out)


def _labels(runs):
    """What to call each payload in an error raised about it.

    A pass holding one segment is that segment, and is named the way it always
    was — most timelines are nothing but these. A pass holding several is named
    by the cards it covers, because that is what the user would go and look at.
    A timeline that is one pass end to end has no card worth singling out.
    """
    if len(runs) == 1 and runs[0][1] - runs[0][0] > 1:
        return ["This one-pass render"]
    return [f"Segment {start + 1}" if end - start == 1 else f"Segments {start + 1}-{end}"
            for start, end in runs]


class MiniMaxH3Timeline(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        import comfy.samplers

        return io.Schema(
            node_id="MiniMaxH3Timeline",
            display_name="MiniMax H3 Timeline",
            category="MiniMax",
            description=(
                "Build a clip out of several shots. Chained: each segment is a full "
                "generation with its own prompt, references and LoRAs, and can start "
                "from the previous one's last frame. One pass: the same segments become "
                "the shots of a single generation, cut times and all."
            ),
            # This node returns a subgraph rather than tensors — see the module
            # docstring for why it cannot be an ordinary node. It is also an
            # output node: it saves the finished clip itself, which is what lets
            # it have no output sockets either.
            enable_expand=True,
            is_output_node=True,
            inputs=[
                # No model sockets. The weights are named in `timeline_data` and
                # `models.emit_links` builds the loaders inside the subgraph —
                # see that module for why that is better than five wires.
                io.String.Input("timeline_data", multiline=True, default=DEFAULT_DATA),
                io.Int.Input("seed", default=0, min=0, max=0xffffffffffffffff, control_after_generate=True,
                    tooltip="Chained: segment k runs on seed + k, so consecutive shots are not the same noise with different prompts. One pass: there is one generation, so it is just the seed."),
                io.Int.Input("steps", default=20, min=1, max=10000),
                # The released H3 checkpoints are CFG-distilled, so guidance is
                # already in the weights and 1.0 is the value they were trained
                # to run at. Left as an ordinary widget: it is a default, not a
                # constraint, and anyone who wants to push it can.
                io.Float.Input("cfg", default=1.0, min=0.0, max=100.0, step=0.1, round=0.01),
                # What the official H3 templates sample with. Left to the combo's
                # own default this would be `euler`, which is simply the first
                # name in core's list — a 20-step H3 render is visibly worse for
                # it, and that is the whole difference between this node and a
                # hand-wired Creator graph copied off the template.
                io.Combo.Input("sampler_name", options=comfy.samplers.KSampler.SAMPLERS,
                               default="res_multistep"),
                io.Combo.Input("scheduler", options=comfy.samplers.KSampler.SCHEDULERS,
                               default="simple",
                               tooltip="The templates use 'simple'; for reference-heavy prompts they suggest 'beta' or 'normal' instead."),
                # Both accelerators are other people's nodes and both are off
                # until asked for — see `accel.py`. They patch the model, so they
                # cost nothing to leave off and nothing here reimplements them.
                io.Combo.Input("block_cache", options=accel.BLOCK_CACHE_MODES, default="off",
                    tooltip="FirstBlockCache: skip the rest of the DiT on steps where the first block barely moved. 'fast' is the pack's recommended preset. Needs ComfyUI-MiniMaxH3-FirstBlockCache."),
                io.Boolean.Input("spectrum", default=False,
                    tooltip="Spectrum: forecast features across steps instead of evaluating every one. Needs ComfyUI-Spectrum-MiniMax-H3. Combines with block_cache; cannot be combined with EasyCache."),
                io.Float.Input("spectrum_blend", default=0.5, min=0.0, max=1.0, step=0.01,
                    tooltip="Spectrum's video spectral share. Higher is faster and further from a native render. Ignored unless 'spectrum' is on."),
            ],
            # Nothing comes out either: the render is saved and shown in the node
            # body, so there is no socket for a graph to hang off.
            outputs=[],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, timeline_data, **kwargs):
        try:
            return (timeline_data, _stamps(json.loads(timeline_data)))
        except Exception:
            return (timeline_data, ())

    @classmethod
    def execute(cls, timeline_data, seed, steps, cfg, sampler_name, scheduler,
                block_cache="off", spectrum=False, spectrum_blend=0.5) -> io.NodeOutput:
        data = _parse(timeline_data)

        # One payload per pass, and a pass is a run of merged segments — usually
        # one segment long. How the timeline is *compiled* is the only thing the
        # merging changes; what is built from the result is the same loop either
        # way. `render.emit` wires each payload to the one before it, and a pass
        # holding several segments simply has no seam inside it to wire.
        payloads = compiler.timeline_payloads(data, image_size_lookup=media.image_size)
        labels = _labels(compiler.timeline_runs(data))

        graph = render.emit(
            payloads, labels,
            models.Weights.from_blob(data),
            render.Sampling(seed=seed, steps=steps, cfg=cfg,
                            sampler_name=sampler_name, scheduler=scheduler),
            accel.Settings(block_cache=block_cache, spectrum=spectrum,
                           spectrum_blend=spectrum_blend),
            cls.hidden.unique_id,
            # Refused before anything is sampled — see MiniMaxH3Creator.execute.
            filename_prefix=outputs.video(data, settings.video_prefix()))
        return render.expanded(graph)


class MiniMaxH3TimelineSegment(io.ComfyNode):
    """One segment of a timeline — the Creator node's job for one shot.

    Written into the graph by `MiniMaxH3Timeline` and not meant to be placed by
    hand. It takes a self-contained payload rather than the timeline plus an
    index, so that its cache key changes when *this* segment changes and not
    when any other one does.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3TimelineSegment",
            display_name="MiniMax H3 Timeline Segment",
            category="MiniMax/internal",
            description="One segment of a MiniMax H3 timeline. Written into the graph by the Timeline node.",
            is_dev_only=True,
            inputs=[
                io.Clip.Input("clip"),
                # Optional because a text-only segment encodes no picture: the
                # video VAE is reached for only when there is a keyframe or a
                # visual reference to turn into a condition latent, so the graph
                # leaves it unwired otherwise and the loader stays a decode-time
                # cost. Absent when it *is* needed raises below rather than
                # reaching a None inside the encoder.
                io.Vae.Input("vae", optional=True),
                # Optional for the same reason on the sound side: nothing on the
                # encode path touches the audio VAE unless the request carries
                # reference audio or a sound seam. The PreStage's still branch
                # emits this node without one either way. Both raise below if it
                # is missing rather than reaching a None.
                io.Vae.Input("audio_vae", optional=True),
                io.String.Input("segment_data", multiline=True),
                io.Model.Input("model_fl2va", optional=True),
                io.Model.Input("model_ref2va", optional=True),
                io.Image.Input("prev_image", optional=True,
                    tooltip="An earlier segment's last frame, when this segment continues from it."),
                io.Audio.Input("prev_audio", optional=True,
                    tooltip="The tail of an earlier segment's soundtrack, when this segment's sound continues from it."),
                io.Image.Input("next_image", optional=True,
                    tooltip="The opening frames of the supplied clip this segment runs into."),
                io.Audio.Input("next_audio", optional=True,
                    tooltip="The opening of that clip's soundtrack, when this segment's sound runs into it."),
            ],
            outputs=[
                io.Model.Output(display_name="model"),
                io.Conditioning.Output(display_name="positive"),
                io.Latent.Output(),
            ],
            # For the "now rendering segment N" report — the announce below
            # names this node, whose id is the Timeline's plus a GraphBuilder
            # prefix, and the stage prefix-matches it back to the node body.
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, segment_data, **kwargs):
        try:
            payload = json.loads(segment_data)
            return (segment_data, _stamps({"segments": [payload.get("request", {})]}))
        except Exception:
            return (segment_data, ())

    @classmethod
    def execute(cls, clip, segment_data, vae=None, audio_vae=None,
                model_fl2va=None, model_ref2va=None,
                prev_image=None, prev_audio=None,
                next_image=None, next_audio=None) -> io.NodeOutput:
        payload = _parse(segment_data)

        # Which segment the queue has reached, told to the stage the moment
        # this segment starts encoding — the sampler that follows reports steps
        # but not whose they are, and on a long strip "23 / 40" says nothing
        # about where in the piece you are. `render.emit` stamps the index onto
        # multi-segment payloads only, so a Creator render announces nothing.
        # A cached segment never executes and so never announces, which is
        # right: the stage should name the segment actually being made.
        progress = payload.get("progress")
        if progress:
            _announce(cls.hidden.unique_id, progress)

        compiled = compiler.compile_segment(payload, image_size_lookup=media.image_size)

        # Both VAEs are wired only when the encoder will actually reach for them
        # (`render` gates on the same two predicates), so a missing one here is a
        # graph that decided this segment needs no encode with it. Named before
        # any of it runs: a hand-built graph should hear which input is missing
        # rather than meet a None inside the encoder.
        if vae is None and compiled.encodes_video():
            raise ValueError(
                "This generation encodes a keyframe or a visual reference, so it "
                "needs the video VAE on 'vae'."
            )
        if audio_vae is None and compiled.encodes_audio():
            raise ValueError(
                "This generation carries sound — reference audio, or a seam "
                "continuing the previous segment's — so it needs the audio VAE "
                "on 'audio_vae'."
            )

        # `prompt_override` replaces the composed prompt verbatim, after
        # compiling — routing, canvas and references are all still worked out
        # from the request, and only the text the DiT reads is swapped. It has no
        # control of its own any more: the node has no sockets, and the refiner's
        # editable rewrite is the same escape hatch with a UI on it. Still read
        # here because a hand-written blob may carry one, and because it lives
        # inside the string this node caches on, so changing it re-runs the
        # generation exactly as editing the prompt would.
        override = payload.get("prompt_override")
        if override:
            compiled.prompt = override

        model = {"fl2va": model_fl2va, "ref2va": model_ref2va}[compiled.checkpoint]
        if model is None:
            raise ValueError(
                f"This segment is {compiled.mode}, which needs the "
                f"{compiled.checkpoint.upper()} checkpoint — connect it to "
                f"'model_{compiled.checkpoint}'."
            )
        model = lora.apply(model, payload["request"].get("loras"), compiled.checkpoint)

        loaded = media.load_all(compiled)
        if compiled.continues:
            if prev_image is None:
                raise ValueError(
                    "This segment continues from an earlier one but no frame "
                    "reached it — the Timeline node should have wired one."
                )
            if prev_image.shape[0] < compiled.feather:
                raise ValueError(
                    f"this seam inherits {compiled.feather} frames but only "
                    f"{prev_image.shape[0]} reached it — shorten the feather "
                    f"or lengthen the source segment"
                )
            loaded[encoder.PREV_FRAME] = {"image": prev_image[-compiled.feather:]}
        if compiled.continues_audio:
            if prev_audio is None:
                raise ValueError(
                    "This segment's sound continues from an earlier one but no "
                    "audio reached it — the Timeline node should have wired some."
                )
            loaded[encoder.PREV_AUDIO] = {"audio": prev_audio}
        if compiled.ends_on:
            if next_image is None:
                raise ValueError(
                    "This segment runs into the clip after it but no frame "
                    "reached it — the Timeline node should have wired one."
                )
            if next_image.shape[0] < compiled.ends_feather:
                raise ValueError(
                    f"this seam blends {compiled.ends_feather} frames of the "
                    f"clip that follows but only {next_image.shape[0]} reached "
                    f"it — shorten the blend, or use more of the clip"
                )
            loaded[encoder.NEXT_FRAME] = {"image": next_image[:compiled.ends_feather]}
        if compiled.ends_on_audio:
            if next_audio is None:
                raise ValueError(
                    "This segment's sound runs into the clip after it but no "
                    "audio reached it — the Timeline node should have wired some."
                )
            loaded[encoder.NEXT_AUDIO] = {"audio": next_audio}
        if (compiled.continues or compiled.continues_audio
                or compiled.ends_on or compiled.ends_on_audio):
            # What core's payload assembly cannot express — keyframes alongside
            # references, guides at real timeline positions — is repaired just
            # before the forward; `payload.py` says exactly what and why. Inert
            # on a seam that needs neither, so every seam wears it rather than
            # this node re-deriving which ones do.
            model = payload_repair.repair(model)

        cond, latent = encoder.encode(clip, vae, audio_vae, compiled, loaded)
        return io.NodeOutput(model, cond, latent)


class MiniMaxH3AudioTail(io.ComfyNode):
    """The end of a decoded soundtrack — what the next segment's sound continues from.

    The picture's counterpart is one frame; sound's is a stretch of it, because a
    single sample says nothing about a room. How long is `compile.DEFAULT_AUDIO_TAIL_S`
    and why it is short is argued there.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3AudioTail",
            display_name="MiniMax H3 Audio Tail",
            category="MiniMax/internal",
            description="The last few seconds of a decoded soundtrack, for the next timeline segment.",
            is_dev_only=True,
            inputs=[
                io.Audio.Input("audio"),
                io.Float.Input("seconds", default=compiler.DEFAULT_AUDIO_TAIL_S,
                               min=0.1, max=compiler.MAX_AUDIO_TAIL_S, step=0.1),
            ],
            outputs=[io.Audio.Output()],
        )

    @classmethod
    def execute(cls, audio, seconds) -> io.NodeOutput:
        waveform = audio["waveform"]
        rate = int(audio["sample_rate"])
        wanted = max(1, int(round(float(seconds) * rate)))
        if waveform.shape[-1] == 0:
            raise ValueError("no audio to continue from")
        # A segment shorter than the tail hands over everything it has rather
        # than being padded: silence we invented is not what came before.
        return io.NodeOutput({"waveform": waveform[..., -wanted:], "sample_rate": rate})


class MiniMaxH3LastFrame(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3LastFrame",
            display_name="MiniMax H3 Last Frame",
            category="MiniMax/internal",
            description="The final frames of a decoded batch — what the next timeline segment continues from.",
            is_dev_only=True,
            inputs=[
                io.Image.Input("image"),
                # A feathered seam inherits a run instead of a single frame.
                # Optional so a classic seam's graph — and its cache keys —
                # look exactly as they always have.
                io.Int.Input("count", default=1, min=1, max=64, optional=True),
            ],
            outputs=[io.Image.Output()],
        )

    @classmethod
    def execute(cls, image, count=1) -> io.NodeOutput:
        count = max(1, int(count))
        if image.shape[0] < count:
            # Padding or repeating frames would pin motion that never happened;
            # the seam's width has to come down instead.
            raise ValueError(
                f"the source segment has {image.shape[0]} frames and this seam "
                f"inherits {count} — shorten the feather or lengthen the source"
                if image.shape[0] else "no frames to continue from"
            )
        return io.NodeOutput(image[-count:])


class MiniMaxH3SeamTrim(io.ComfyNode):
    """A blended segment minus the runs it shares with its neighbours.

    A blend pins context frames on this segment's own timeline and
    re-generates them there, so an untrimmed segment would play them twice —
    once here and once in the pass they came from. `frames` is the run
    inherited from the pass in front, at the head; `tail` is the opening of a
    supplied clip this segment runs into, at the end. Trimmed after decode —
    picture and the matching stretch of sound together, so the two stay in
    phase across the cut.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3SeamTrim",
            display_name="MiniMax H3 Seam Trim",
            category="MiniMax/internal",
            description="Drops a blended seam's re-generated overlap off a decoded segment.",
            is_dev_only=True,
            inputs=[
                io.Image.Input("images"),
                io.Audio.Input("audio"),
                io.Int.Input("frames", default=0, min=0, max=64),
                # Optional, so a head-only trim's node inputs — and its cache
                # key — are byte-identical to what they were before the seam
                # could run the other way.
                io.Int.Input("tail", default=0, min=0, max=64, optional=True),
            ],
            outputs=[io.Image.Output(display_name="images"), io.Audio.Output(display_name="audio")],
        )

    @classmethod
    def execute(cls, images, audio, frames, tail=0) -> io.NodeOutput:
        frames, tail = max(0, int(frames)), max(0, int(tail))
        if not frames and not tail:
            return io.NodeOutput(images, audio)
        if images.shape[0] <= frames + tail:
            # compile refuses a blend of half the segment or more, so hitting
            # this means the graph was built against different arithmetic.
            raise ValueError(
                f"cannot trim {frames + tail} blended frames off a "
                f"{images.shape[0]}-frame segment"
            )
        rate = int(audio["sample_rate"])
        head_samples = int(round(frames / canvas.FPS * rate))
        # Counted off the end rather than as an absolute index: the decoded
        # soundtrack is the same span as the picture but not the same length,
        # and an index computed from the frame count would drift by the
        # rounding.
        tail_samples = int(round(tail / canvas.FPS * rate))
        waveform = audio["waveform"][..., head_samples:]
        if tail_samples:
            waveform = waveform[..., :-tail_samples]
        return io.NodeOutput(
            images[frames:images.shape[0] - tail] if tail else images[frames:],
            {"waveform": waveform, "sample_rate": rate},
        )


class MiniMaxH3Reel(io.ComfyNode):
    """The passes so far, as a list of what to play — not as one clip.

    This is what replaced `MiniMaxH3TimelineJoin`, and the difference is the
    whole reason it exists. The join concatenated: it took two passes and
    returned the tensor holding both, so folding N passes built N-1 running
    totals and ComfyUI kept every one of them alive. Ten 768p passes came to
    about 81 GB of intermediates on top of 15 GB of passes, which is O(N^2) in
    the length of the piece.

    A reel copies nothing. It carries references to the tensors the decoders
    already produced, in play order, and `mux.py` walks it writing one frame at
    a time. So the fold costs a list, the file is written from parts that never
    have to be adjacent in memory, and the peak is the passes themselves.

    Chained the same way the join was — each reel node takes the one before it —
    because that keeps the growth an ordinary graph edge with no variadic
    inputs, and it keeps a pass's cache key naming exactly the passes in front
    of it.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3Reel",
            display_name="MiniMax H3 Reel",
            category="MiniMax/internal",
            description="Adds one pass's frames and sound to the reel the save node writes.",
            is_dev_only=True,
            inputs=[
                io.Image.Input("images"),
                io.Audio.Input("audio"),
                io.Custom(REEL_TYPE).Input("reel", optional=True,
                    tooltip="The passes in front of this one. Absent on the first."),
            ],
            outputs=[io.Custom(REEL_TYPE).Output(display_name="reel")],
        )

    @classmethod
    def execute(cls, images, audio, reel=None) -> io.NodeOutput:
        # A new list rather than an append: the reel this was handed is another
        # node's cached output, and growing it in place would rewrite history
        # every time a later pass re-ran.
        return io.NodeOutput([*(reel or []), {"images": images, "audio": audio}])


class MiniMaxH3ClipReel(io.ComfyNode):
    """Supplied footage, added to the reel as a file rather than as frames.

    The one node in the chain that decodes nothing. A clip card is part of the
    finished video, and the finished video is written frame by frame — so the
    file only has to be *named* here and `mux.py` demuxes, conforms and
    re-encodes it straight into the container. Two minutes of 768p footage
    would be 35 GB as a tensor; this way it is a dict.

    The audio VAE is taken as an input for one number: the rate its decoder
    outputs at. That is the rate the generated passes' sound arrives at, so it
    is the rate this clip has to be resampled to, and it is a fact about the
    weights on this disk rather than a constant this package may assume.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3ClipReel",
            display_name="MiniMax H3 Clip",
            category="MiniMax/internal",
            description="Adds a supplied clip to the reel, without decoding it.",
            is_dev_only=True,
            inputs=[
                io.String.Input("clip_data", multiline=True),
                io.Custom(REEL_TYPE).Input("reel", optional=True),
                io.Vae.Input("audio_vae", optional=True,
                    tooltip="Only for its output sample rate — the clip's sound is "
                            "resampled to whatever the generated passes decode at."),
            ],
            outputs=[io.Custom(REEL_TYPE).Output(display_name="reel")],
        )

    @classmethod
    def fingerprint_inputs(cls, clip_data, **kwargs):
        try:
            return (clip_data, _stamps({"segments": [json.loads(clip_data)]}))
        except Exception:
            return (clip_data, ())

    @classmethod
    def execute(cls, clip_data, reel=None, audio_vae=None) -> io.NodeOutput:
        spec = dict(_parse(clip_data))
        # Resolved here rather than in `mux.py`, which knows nothing about
        # ComfyUI's folders and is loadable on its own because of it.
        spec["name"] = spec["filename"]
        spec["path"] = media.resolve(spec.pop("filename"))
        if spec.get("sound"):
            if audio_vae is None:
                # The graph wires it whenever the clip plays with its sound, so
                # reaching here means a hand-built graph — say which input is
                # missing rather than writing the clip at the wrong pitch.
                raise ValueError(
                    "this clip plays with its sound, so it needs the audio VAE "
                    "on 'audio_vae' to know what rate to resample it to."
                )
            spec["rate"] = mux.decode_sample_rate(audio_vae)
            spec["channels"] = mux.decode_channels(audio_vae)
        return io.NodeOutput([*(reel or []), {"clip": spec}])


class MiniMaxH3ClipFrames(io.ComfyNode):
    """The frames a seam beside a supplied clip inherits.

    The counterpart to `MiniMaxH3LastFrame`, for a pass that has no decoded
    frames because it was never generated. `at` says which end: the tail is
    what a generation after the clip continues from, the head is what a
    generation *before* it ends on.

    Its own node rather than an output of the clip's reel node, so that a clip
    nothing continues from is never decoded at all. What is decoded here is the
    seam's width — one frame, or a feathered run of at most 39.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3ClipFrames",
            display_name="MiniMax H3 Clip Frames",
            category="MiniMax/internal",
            description="The first or last frames of a supplied clip, for a seam beside it.",
            is_dev_only=True,
            inputs=[
                io.String.Input("clip_data", multiline=True),
                io.Int.Input("count", default=1, min=1, max=64),
                io.Combo.Input("at", options=["head", "tail"], default="tail"),
            ],
            outputs=[io.Image.Output()],
        )

    @classmethod
    def fingerprint_inputs(cls, clip_data, **kwargs):
        try:
            return (clip_data, _stamps({"segments": [json.loads(clip_data)]}))
        except Exception:
            return (clip_data, ())

    @classmethod
    def execute(cls, clip_data, count=1, at="tail") -> io.NodeOutput:
        return io.NodeOutput(media.clip_frames(_parse(clip_data), int(count), at))


class MiniMaxH3ClipAudio(io.ComfyNode):
    """The sound a seam beside a supplied clip inherits. See `MiniMaxH3ClipFrames`."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3ClipAudio",
            display_name="MiniMax H3 Clip Audio",
            category="MiniMax/internal",
            description="The first or last seconds of a supplied clip's sound, for a seam beside it.",
            is_dev_only=True,
            inputs=[
                io.String.Input("clip_data", multiline=True),
                io.Float.Input("seconds", default=compiler.DEFAULT_AUDIO_TAIL_S,
                               min=0.1, max=compiler.MAX_AUDIO_TAIL_S, step=0.1),
                io.Combo.Input("at", options=["head", "tail"], default="tail"),
            ],
            outputs=[io.Audio.Output()],
        )

    @classmethod
    def fingerprint_inputs(cls, clip_data, **kwargs):
        try:
            return (clip_data, _stamps({"segments": [json.loads(clip_data)]}))
        except Exception:
            return (clip_data, ())

    @classmethod
    def execute(cls, clip_data, seconds=compiler.DEFAULT_AUDIO_TAIL_S,
                at="tail") -> io.NodeOutput:
        return io.NodeOutput(media.clip_audio(_parse(clip_data), float(seconds), at))


class MiniMaxH3Save(io.ComfyNode):
    """The last node of every render: the reel, muxed and written out.

    Ours rather than core's `CreateVideo` + `SaveVideo` for one mechanical
    reason: `SaveVideo`'s `codec` is a `DynamicCombo`, whose value the frontend
    assembles out of a dynamic schema. A graph built in Python has no frontend,
    so there is nothing to assemble it and the input arrives as a bare string the
    node then subscripts.

    It takes a reel rather than one clip's tensors, and `mux.py` writes it part
    by part — which is what stops a long timeline from having to exist as one
    concatenated tensor first. That also retired the CRF version gate this node
    used to carry: `VideoFromComponents.save_to` only learned `crf` in ComfyUI
    0.29, so a quality setting had to be refused on anything older. Writing the
    container ourselves, it is always honoured.

    It is an output node, and `render.emit_tail` stamps the calling node's id on
    it, so what it saves is reported against the Creator or Timeline the user is
    looking at rather than against an expanded node on nobody's canvas.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3Save",
            display_name="MiniMax H3 Save",
            category="MiniMax/internal",
            description="Writes a render's passes into one file under output/.",
            is_dev_only=True,
            is_output_node=True,
            inputs=[
                io.Custom(REEL_TYPE).Input("reel"),
                io.Float.Input("fps", default=float(canvas.FPS), min=1.0, max=120.0),
                io.String.Input("filename_prefix", default="minimax/H3"),
                # An input rather than a read of `settings.py` here, so that
                # changing the quality and re-queueing actually re-writes the
                # file: an output node whose inputs are all unchanged is a
                # cache hit, and the render would keep the quality it had.
                # `render.emit_tail` is the one place that reads the setting.
                io.Int.Input("crf", default=settings.DEFAULT_CRF,
                             min=settings.MIN_CRF, max=settings.MAX_CRF),
            ],
            outputs=[],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
        )

    @classmethod
    def execute(cls, reel, fps, filename_prefix,
                crf=settings.DEFAULT_CRF) -> io.NodeOutput:
        import os

        import folder_paths
        from comfy.cli_args import args

        width, height = mux.reel_geometry(reel)
        directory, name, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory(), width, height)

        # The workflow, so a render dropped back onto the canvas rebuilds the node
        # that made it. Same two hidden fields core's savers write, and skipped
        # under --disable-metadata for the same reason.
        metadata = None
        if not args.disable_metadata:
            collected = dict(cls.hidden.extra_pnginfo or {})
            if cls.hidden.prompt is not None:
                collected["prompt"] = cls.hidden.prompt
            metadata = collected or None

        filename = f"{name}_{counter:05}_.mp4"
        mux.write(os.path.join(directory, filename), reel,
                  fps=float(fps), crf=int(crf), metadata=metadata)

        # Not `ui.PreviewVideo`: that reports under "images", the key the stock
        # frontend preview keys on — and with the caller's id stamped on this
        # node, that stock player lands on the canvas node right under the
        # stage already showing the same clip. A key core does not know keeps
        # the report and loses the widget; stage.js reads it by name.
        return io.NodeOutput(ui={"mmc_video": [
            {"filename": filename, "subfolder": subfolder, "type": "output"},
        ]})


# Registered by `creator_node.MiniMaxCreatorExtension` — one extension for the
# package, so there is one place that says what this node pack contains.
NODES = [MiniMaxH3Timeline, MiniMaxH3TimelineSegment, MiniMaxH3LastFrame,
         MiniMaxH3SeamTrim, MiniMaxH3AudioTail, MiniMaxH3Reel,
         MiniMaxH3ClipReel, MiniMaxH3ClipFrames, MiniMaxH3ClipAudio,
         MiniMaxH3Save]
