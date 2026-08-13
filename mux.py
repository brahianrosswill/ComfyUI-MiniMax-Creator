"""The finished piece, written part by part into one mp4.

A render is several generations played end to end, and until now they were
*concatenated* to become one: `MiniMaxH3TimelineJoin` folded the passes
pairwise and the save node was handed the single tensor that came out. That
fold is the most expensive thing in a long timeline, and not by a little.

Every intermediate of a pairwise fold is a node output, and ComfyUI keeps node
outputs for the whole execution — so the running totals all stay alive at once.
A 768p frame is 12.4 MB of float32 and a 124-frame pass is 1.5 GB, which makes
ten passes about 81 GB of intermediates on top of the 15 GB of passes. It is
O(N^2) in the length of the piece. Worse, the default cache
(`RAMPressureCache`) evicts current-generation entries over 512 MB when memory
runs short, and re-running an evicted join means re-running what fed it, which
upstream of a join is a KSampler.

Nothing about a video file needs that. An mp4 is written frame by frame, so the
parts only ever have to be *reachable in order* — never adjacent in memory. So
the passes are collected into a reel (`MiniMaxH3Reel`, a list of references
that copies nothing) and this module walks it, encoding each part into one open
container. Peak memory is the passes themselves, with no concatenation buffer
at all, and the frames of a part are released as the encoder consumes them.

Ours rather than core's `VideoFromComponents.save_to`, which this is otherwise
a close copy of: that one takes a single tensor, so using it would mean
building the very thing this exists to avoid. Writing the container here also
retires the CRF version gate — `save_to` only learned `crf` in ComfyUI 0.29 and
the save node had to refuse a quality setting it could not honour on anything
older. This one always can.

The audio is written part by part too, and each part's soundtrack is held to
its own picture's length. That is not tidiness: the parts are laid end to end,
so a part whose sound runs short by 30 ms does not lose 30 ms, it shifts
everything after it by 30 ms and the drift accumulates down the reel. Sound is
padded with silence or cut to fit, and only ever by the rounding between a
frame count and a sample count.
"""

import json
from fractions import Fraction

import numpy as np
import torch

# The layouts PyAV names, by channel count. Anything else is refused rather
# than guessed at: picking a layout decides which speaker each channel goes to.
_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1"}

# How much sound is handed to the encoder at once. Long enough that the call
# overhead is nothing, short enough that a ten-minute soundtrack is never
# converted to a numpy array in one piece.
_AUDIO_CHUNK_S = 1.0


class MuxError(ValueError):
    """The parts of a reel cannot be written as one file."""


def _geometry(images):
    return int(images.shape[2]), int(images.shape[1])


def reel_geometry(parts):
    """(width, height) of a reel, refusing one whose parts disagree.

    The timeline pins one canvas across every pass precisely so this cannot
    happen — this is the check the pairwise join used to make, kept because it
    is the one that says something went wrong upstream rather than that the
    encoder is unhappy.
    """
    if not parts:
        raise MuxError("nothing to save: the reel is empty")
    width, height = _geometry(parts[0]["images"])
    for index, part in enumerate(parts[1:], start=2):
        other_w, other_h = _geometry(part["images"])
        if (other_w, other_h) != (width, height):
            raise MuxError(
                f"part {index} is {other_w}x{other_h} and part 1 is "
                f"{width}x{height} — the parts of one render have to match"
            )
    return width, height


def _audio_format(parts):
    """(sample_rate, channels) for the reel, refusing parts that disagree.

    Read off the reel rather than assumed: the rate is the audio VAE's output
    rate, which is a fact about the weights on this disk and not a constant
    this package gets to pick.
    """
    rate = channels = None
    for index, part in enumerate(parts, start=1):
        audio = part.get("audio")
        if audio is None:
            continue
        part_rate = int(audio["sample_rate"])
        part_channels = int(audio["waveform"].shape[1])
        if rate is None:
            rate, channels = part_rate, part_channels
        elif (part_rate, part_channels) != (rate, channels):
            raise MuxError(
                f"part {index} has {part_channels} channels at {part_rate} Hz "
                f"and the reel is {channels} at {rate} — the parts of one "
                f"render have to match"
            )
    if rate is not None and channels not in _LAYOUTS:
        raise MuxError(f"cannot write {channels}-channel audio")
    return rate, channels


def _fit(waveform, samples):
    """One part's sound, held to exactly the length of its own picture.

    Cut when it overruns, padded with silence when it falls short. Both are
    rounding between a frame count and a sample count — a generated part's two
    halves are the same span by construction — and the alternative is not
    "faithful", it is every later part sliding by the difference.
    """
    have = waveform.shape[-1]
    if have > samples:
        return waveform[..., :samples]
    if have < samples:
        pad = torch.zeros(waveform.shape[:-1] + (samples - have,), dtype=waveform.dtype)
        return torch.cat([waveform, pad], dim=-1)
    return waveform


def write(path, parts, fps, crf, metadata=None):
    """Write a reel to `path` as one H.264/AAC mp4. -> (width, height).

    `parts` is the reel: `[{"images": IMAGE, "audio": AUDIO or None}, ...]` in
    play order. One container, one video stream and one audio stream, opened
    once and fed part by part — the encoder is never flushed between parts, so
    what comes out is one continuous stream rather than files stitched together.
    """
    import av

    width, height = reel_geometry(parts)
    rate, channels = _audio_format(parts)
    frame_rate = Fraction(round(float(fps)))
    video_time_base = Fraction(1, frame_rate.numerator)
    pix_fmt = "yuv420p"

    # Same flags core writes: metadata tags survive, and faststart puts the
    # index at the front so the stage can play the file as it downloads.
    with av.open(path, mode="w", options={"movflags": "use_metadata_tags+faststart"}) as output:
        # Before any stream, like core's savers — the workflow rides in the
        # container so a render dropped back on the canvas rebuilds its node.
        for key, value in (metadata or {}).items():
            output.metadata[key] = value if isinstance(value, str) else json.dumps(value)

        video = output.add_stream("h264", rate=frame_rate)
        video.width, video.height, video.pix_fmt = width, height, pix_fmt
        video.options = {"crf": str(int(crf))}
        video.codec_context.time_base = video_time_base

        audio = None
        if rate is not None:
            layout = _LAYOUTS[channels]
            audio = output.add_stream("aac", rate=rate, layout=layout)
            audio_time_base = Fraction(1, rate)

        written_frames = 0
        written_samples = 0

        for part in parts:
            images = part["images"]
            for index in range(images.shape[0]):
                array = (images[index] * 255).clamp(0, 255).byte().cpu().numpy()
                frame = av.VideoFrame.from_ndarray(array, format="rgb24")
                frame = frame.reformat(format=pix_fmt)
                frame.pts = written_frames + index
                frame.time_base = video_time_base
                output.mux(video.encode(frame))
            written_frames += images.shape[0]

            if audio is None:
                continue
            # The part's own sound, held to the part's own picture — see `_fit`.
            # A part with no soundtrack at all in a reel that has one is silence
            # of exactly its own length, which is the only thing that keeps the
            # parts after it where they belong.
            wanted = int(round(images.shape[0] / float(frame_rate) * rate))
            sound_in = part.get("audio")
            waveform = (sound_in["waveform"][0].float().cpu() if sound_in is not None
                        else torch.zeros(channels, 0))
            waveform = _fit(waveform, wanted)
            chunk = max(1, int(_AUDIO_CHUNK_S * rate))
            for start in range(0, wanted, chunk):
                block = waveform[..., start:start + chunk].contiguous().numpy()
                sound = av.AudioFrame.from_ndarray(
                    np.ascontiguousarray(block), format="fltp", layout=layout)
                sound.sample_rate = rate
                sound.pts = written_samples + start
                sound.time_base = audio_time_base
                output.mux(audio.encode(sound))
            written_samples += wanted

        # Flushed once, at the end of the reel rather than at the end of each
        # part: a flush closes out the encoder's lookahead, and doing it per
        # part would put a keyframe and a GOP boundary at every join.
        output.mux(video.encode(None))
        if audio is not None:
            output.mux(audio.encode(None))

    return width, height
