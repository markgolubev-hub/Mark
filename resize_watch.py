#!/usr/bin/env python3
"""Когда в MAIN появляется ролик, сразу пишет ресайзы в resize.

Имя: <имя>_1080x1920_30s.mp4 и _59s.mp4, так же 1080x1080 и 1920x1080.
Картинка заполняет кадр. Если ролик короче нужной длины, он запускается
заново через белую вспышку: 5 кадров в белый и 10 кадров затухания.
Музыка из папки sound играет с начала и затухает с 27-й секунды
на ролике 30 с и с 56-й секунды на ролике 59 с.
Пэкшот из папки packshot:
30 с — с 27-й секунды 15-го кадра, 59 с — с 56-й секунды. 30 fps.

Папка edits: несколько роликов собираются во все порядки.
30 с — время делится поровну (3 ролика по 10 с, 2 по 15 с).
59 с и 3 и больше роликов — тоже поровну (три ролика почти по 20 с).
59 с и 1–2 ролика — каждый целиком, если короче 59 с, круг повторяется.
Между кусками та же белая вспышка. Имя: edit_<порядок>_1080x1920_30s.mp4.

Папка edit_9x16: все порядки.
2 ролика — 30 с по 15 с. Короткий внутри своих 15 с запускается заново.
59 с — заход не длиннее 30 с. Ролик короче 30 с берётся целиком.
Секунда до 59 срезается со второго (30 с + 29 с). Если одного не хватает,
хвост добирает тот, где ещё есть кадры. Если обоих мало, круг повторяется.
3 ролика — 30 с по 10 с, 59 с по 20 с (последний 19 с).
4 ролика — 59 с по 15 с (последний 14 с). Четыре по 10 с в 30 с не входят,
на 30 с они делятся поровну, по 7,5 с.
Короткий ролик внутри слота 10, 15 или 20 с запускается заново.
Имя: edit_9x16_<порядок>_1080x1920_30s.mp4.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from itertools import permutations
from pathlib import Path

ROOT = Path("/Users/mark/Documents/work/render")
MAIN = ROOT / "MAIN"
OUT = ROOT / "resize"
PACK = ROOT / "packshot"
SOUND = ROOT / "sound"
EDITS = ROOT / "edits"
EDIT_9X16 = ROOT / "edit_9x16"
EDIT_OUTPUT_RE = re.compile(r"^edit_(?!9x16_).+_\d+x\d+_\d+s(?:\.part)?\.mp4$")
EDIT_9X16_OUTPUT_RE = re.compile(r"^edit_9x16_.+_\d+x\d+_\d+s(?:\.part)?\.mp4$")
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".aiff", ".aif", ".flac", ".ogg"}
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
STATE = Path.home() / "Library/Application Support/render-resize"
LOCK = STATE / "resize_watch.lock"
LOG = STATE / "resize_watch.log"
FPS = 30
PLAN_VERSION = "2x15"
SIZES = ((1080, 1920), (1080, 1080), (1920, 1080))
# (длина в секундах, кадр, с которого начинается пэкшот)
VERSIONS = ((30, 27 * FPS + 15), (59, 56 * FPS))
FLASH_IN = 5
FLASH_OUT = 10
EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
_seen_log = {}
SCRIPT_PATH = Path(__file__).resolve()
try:
    SCRIPT_STAMP = SCRIPT_PATH.stat().st_mtime_ns
except OSError:
    SCRIPT_STAMP = None


def log(message):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + message
    print(line, flush=True)
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_once(key, message):
    if _seen_log.get(key) == message:
        return
    _seen_log[key] = message
    log(message)


def script_replaced():
    if SCRIPT_STAMP is None:
        return False
    try:
        return SCRIPT_PATH.stat().st_mtime_ns != SCRIPT_STAMP
    except OSError:
        return False


def sources():
    if not MAIN.is_dir():
        return []
    found = []
    for path in sorted(MAIN.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in EXTS:
            continue
        found.append(path)
    return found


def output_path(src, width, height, seconds):
    return OUT / f"{src.stem}_{width}x{height}_{seconds}s.mp4"


def videos_in(folder):
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in EXTS:
            found.append(path)
    return found


def audios_in(folder):
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in AUDIO_EXTS:
            found.append(path)
    return found


def file_stamp(path):
    stat = path.stat()
    # mtime у скопированного файла часто старый, ctime — момент, когда он попал в папку
    return max(stat.st_mtime, stat.st_ctime)


def find_music(src):
    tracks = audios_in(SOUND)
    if not tracks:
        return None
    stem = src.stem.lower()
    for path in tracks:
        name = path.stem.lower()
        if name in {stem, f"sound_{stem}", f"{stem}_sound"} or name.endswith("_" + stem):
            return path
    return max(tracks, key=file_stamp)


def find_packshot(src):
    videos = videos_in(PACK)
    stem = src.stem.lower()
    for path in videos:
        name = path.stem.lower()
        if name in {stem, f"packshot_{stem}", f"{stem}_packshot"} or name.endswith("_" + stem):
            return path
    if len(videos) == 1:
        return videos[0]
    return None


def needs_encode(src):
    stamps = [src.stat().st_mtime]
    pack = find_packshot(src)
    if pack is not None:
        stamps.append(pack.stat().st_mtime)
    music = find_music(src)
    if music is not None:
        stamps.append(file_stamp(music))
    newest = max(stamps)
    for width, height in SIZES:
        for seconds, _start in VERSIONS:
            dest = output_path(src, width, height, seconds)
            if not dest.is_file() or dest.stat().st_size <= 0:
                return True
            if dest.stat().st_mtime < newest:
                return True
    return False


def readiness(path):
    stat = path.stat()
    age = time.time() - stat.st_mtime
    if stat.st_size > 0 and age >= 2:
        return "ready"
    if stat.st_size == 0 and age > 60:
        return "skip"
    return "wait"


def media_duration(path):
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip() or 0)
    except ValueError:
        return 0


def probe(path):
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,nb_frames,avg_frame_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout or "{}")
    duration = float((data.get("format") or {}).get("duration") or 0)
    video = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video"), {})
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rate = str(video.get("avg_frame_rate") or "0/1")
    num, den = rate.split("/")
    fps = float(num) / float(den) if float(den) else FPS
    frames = int(video.get("nb_frames") or 0)
    if frames <= 0 and duration > 0:
        frames = int(round(duration * (fps or FPS)))
    audio = any(s.get("codec_type") == "audio" for s in data.get("streams") or [])
    return width, height, frames, audio


def fade_filters(length, to_white, from_white):
    fade_out_n = min(FLASH_IN, length // 2) if to_white else 0
    fade_in_n = min(FLASH_OUT, length - fade_out_n) if from_white else 0
    filters = []
    if fade_in_n:
        filters.append(f"fade=t=in:start_frame=0:nb_frames={fade_in_n}:color=white")
    if fade_out_n:
        start = length - fade_out_n
        filters.append(f"fade=t=out:start_frame={start}:nb_frames={fade_out_n}:color=white")
    return filters


def flash_chain(length, to_white, from_white):
    return ",".join(
        [f"fps={FPS}", "setsar=1", "format=yuv420p", *fade_filters(length, to_white, from_white)]
    )


def loop_graph(src_w, src_h, src_frames, target_frames):
    if src_frames >= target_frames:
        lengths = [target_frames]
        flags = [(False, False)]
    else:
        lengths = []
        flags = []
        remaining = target_frames
        while remaining > 0:
            take = min(src_frames, remaining)
            is_first = not lengths
            is_last = take == remaining
            lengths.append(take)
            flags.append((not is_last, not is_first))
            remaining -= take
    count = len(lengths)
    parts = []
    if count > 1:
        labels = "".join(f"[s{i}]" for i in range(count))
        parts.append(f"[0:v]split={count}{labels}")
    labels_out = []
    for index, (length, (to_white, from_white)) in enumerate(zip(lengths, flags)):
        chain = flash_chain(length, to_white, from_white)
        src_label = "[0:v]" if count == 1 else f"[s{index}]"
        labels_out.append(f"[v{index}]")
        parts.append(
            f"{src_label}trim=end_frame={length},setpts=PTS-STARTPTS,{chain}[v{index}]"
        )
    joined = "".join(labels_out)
    parts.append(
        f"{joined}concat=n={count}:v=1:a=0,trim=end_frame={target_frames},setpts=PTS-STARTPTS[looped]"
    )
    return ";".join(parts)


def cover(width, height, label_in, label_out):
    return (
        f"{label_in}scale={width}:{height}:force_original_aspect_ratio=increase:"
        f"force_divisible_by=2:flags=lanczos,crop={width}:{height},setsar=1,fps={FPS}{label_out}"
    )


def build_command(src, pack, music, src_w, src_h, src_frames, src_audio, pack_audio, width, height, seconds, pack_frame):
    target_frames = seconds * FPS
    delay = pack_frame / FPS
    pack_frames = target_frames - pack_frame
    graph = [loop_graph(src_w, src_h, src_frames, target_frames)]
    if pack is not None:
        graph.append(
            f"[1:v]trim=end_frame={pack_frames},setpts=PTS-STARTPTS,fps={FPS},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos,"
            f"crop={width}:{height},setsar=1,format=rgba,setpts=PTS+{delay}/TB[pk]"
        )
        graph.append(cover(width, height, "[looped]", "[base]"))
        graph.append(
            "[base][pk]overlay=0:0:eof_action=pass:format=auto,fps=30,format=yuv420p[vout]"
        )
    else:
        graph.append(cover(width, height, "[looped]", "[vout]"))
    audio_parts = []
    cmd = [FFMPEG, "-y", "-i", str(src)]
    if pack is not None:
        cmd += ["-t", f"{pack_frames / FPS + 0.25:.3f}", "-i", str(pack)]
    music_index = None
    if music is not None:
        music_index = 1 if pack is None else 2
        cmd += ["-i", str(music)]
    if music is not None:
        fade_at = 27.0 if seconds <= 30 else 56.0
        fade_for = max(seconds - fade_at, 0.1)
        music_samples = max(48000, int(48000 * (media_duration(music) + 1)))
        graph.append(
            f"[{music_index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"aloop=loop=-1:size={music_samples},atrim=0:{seconds},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={fade_at:.3f}:d={fade_for:.3f}[mus]"
        )
        audio_parts.append("[mus]")
    if pack is not None and pack_audio:
        graph.append(
            f"[1:a]atrim=0:{pack_frames / FPS:.3f},asetpts=PTS-STARTPTS,"
            f"adelay={int(round(delay * 1000))}|{int(round(delay * 1000))},"
            f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[pa]"
        )
        audio_parts.append("[pa]")
    elif src_audio and music is None:
        graph.append(
            f"[0:a]atrim=0:{seconds},asetpts=PTS-STARTPTS,apad=whole_dur={seconds},aresample=48000[sa]"
        )
        audio_parts.append("[sa]")
    if len(audio_parts) > 1:
        graph.append(
            f"{''.join(audio_parts)}amix=inputs={len(audio_parts)}:duration=longest:"
            f"dropout_transition=0:normalize=0,atrim=0:{seconds},asetpts=PTS-STARTPTS[aout]"
        )
        audio_label = "[aout]"
    elif audio_parts:
        audio_label = audio_parts[0]
    else:
        audio_label = None
    dest = output_path(src, width, height, seconds)
    tmp = dest.with_name(dest.stem + ".part.mp4")
    cmd += [
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[vout]",
    ]
    if audio_label:
        cmd += ["-map", audio_label, "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-color_range",
        "tv",
        "-movflags",
        "+faststart",
        "-frames:v",
        str(target_frames),
        "-f",
        "mp4",
        str(tmp),
    ]
    return dest, tmp, cmd


def natural_key(path):
    chunks = re.split(r"(\d+)", path.name.lower())
    key = []
    for chunk in chunks:
        if chunk.isdigit():
            key.append((0, int(chunk)))
        else:
            key.append((1, chunk))
    return key


def edit_sources(folder=None):
    return sorted(videos_in(folder or EDITS), key=natural_key)


def is_rendered_name(path):
    name = path.name
    if EDIT_OUTPUT_RE.match(name) or EDIT_9X16_OUTPUT_RE.match(name):
        return True
    return re.search(r"_\d+x\d+_\d+s$", path.stem.lower()) is not None


def is_packshot_name(path):
    stem = path.stem.lower().replace(" ", "").replace("-", "").replace("_", "")
    return any(token in stem for token in ("packshot", "endcard", "пэкшот", "пекшот"))


def dedupe_stems(files):
    chosen = {}
    for path in files:
        key = path.stem.lower()
        prev = chosen.get(key)
        if prev is None or file_stamp(path) >= file_stamp(prev):
            if prev is not None:
                log_once(
                    f"dup-{key}",
                    f"дубль {prev.name} не беру, остаётся {path.name}",
                )
            chosen[key] = path
        else:
            log_once(
                f"dup-{key}",
                f"дубль {path.name} не беру, остаётся {prev.name}",
            )
    return sorted(chosen.values(), key=natural_key)


def select_edit_clips(files):
    """Ролики для склейки и пэкшот, если он лежит в той же папке.

    Готовый рендер и карточка не считаются третьим роликом: иначе два
    ролика превращаются в три и на 30 с каждый идёт по 10 с.
    """
    pack = None
    clips = []
    for path in files:
        if is_rendered_name(path):
            continue
        if is_packshot_name(path):
            pack = path
            continue
        clips.append(path)
    clips = dedupe_stems(clips)
    if pack is None and len(clips) >= 3:
        short = []
        long = []
        for path in clips:
            duration = media_duration(path)
            if 0 < duration <= 3:
                short.append(path)
            else:
                long.append(path)
        if len(short) == 1 and len(long) >= 2:
            pack = short[0]
            clips = long
    return clips, pack


def edit_output_path(order, width, height, seconds, name_prefix="edit"):
    name = "+".join(path.stem for path in order)
    return OUT / f"{name_prefix}_{name}_{width}x{height}_{seconds}s.mp4"


def plan_pieces(frame_counts, seconds):
    """Куски (индекс ролика, число кадров) на ровно seconds * FPS.

    30 с и случай «3 и больше роликов на 59 с» — слоты поровну.
    Короткий ролик внутри своего слота запускается заново.
    59 с и 1–2 ролика — целиком по кругу, последний кусок обрезается.
    """
    target = seconds * FPS
    count = len(frame_counts)
    if count == 0 or target <= 0:
        return []
    use_full = seconds >= 59 and count <= 2
    pieces = []
    if use_full:
        remaining = target
        while remaining > 0:
            for index, frames in enumerate(frame_counts):
                if remaining <= 0:
                    break
                take = min(max(frames, 1), remaining)
                pieces.append((index, take))
                remaining -= take
        return pieces
    base, extra = divmod(target, count)
    for index, frames in enumerate(frame_counts):
        left = base + (1 if index < extra else 0)
        src_frames = max(frames, 1)
        while left > 0:
            take = min(src_frames, left)
            pieces.append((index, take))
            left -= take
    return pieces


def plan_capped_slots(frame_counts, seconds, slot_frames):
    """Каждый ролик занимает slot_frames кадров. Короткий внутри слота запускается заново.

    20 с × 3 и 15 с × 4 дают 60 с. Лишняя секунда срезается с хвоста,
    чтобы вышло ровно seconds.
    """
    target = seconds * FPS
    pieces = []
    for index, frames in enumerate(frame_counts):
        src = max(int(frames), 1)
        left = slot_frames
        while left > 0:
            take = min(src, left)
            pieces.append((index, take))
            left -= take
    overflow = sum(take for _index, take in pieces) - target
    while overflow > 0 and pieces:
        index, take = pieces[-1]
        if take > overflow:
            pieces[-1] = (index, take - overflow)
            overflow = 0
        else:
            overflow -= take
            pieces.pop()
    return [(index, take) for index, take in pieces if take > 0]


def plan_pair_9x16(frame_counts, seconds):
    """Ролики из edit_9x16 на ровно seconds * FPS.

    2 ролика: 30 с по 15 с; 59 с до 30 с, короткий берётся целиком.
    3 ролика: 30 с по 10 с, 59 с по 20 с.
    4 ролика: 59 с по 15 с. На 30 с четыре по 10 с не входят, деление поровну.
    """
    count = len(frame_counts)
    if count == 2 and seconds < 59:
        return plan_capped_slots(frame_counts, seconds, 15 * FPS)
    if count == 3 and seconds < 59:
        return plan_capped_slots(frame_counts, seconds, 10 * FPS)
    if count == 3 and seconds >= 59:
        return plan_capped_slots(frame_counts, seconds, 20 * FPS)
    if count == 4 and seconds >= 59:
        return plan_capped_slots(frame_counts, seconds, 15 * FPS)
    if count != 2 or seconds < 59:
        return plan_pieces(frame_counts, seconds)
    target = seconds * FPS
    cap = 30 * FPS
    src = [max(int(frames), 1) for frames in frame_counts]
    takes = [min(frames, cap) for frames in src]
    extra = sum(takes) - target
    if extra > 0:
        for index in range(len(takes) - 1, -1, -1):
            cut = min(max(takes[index] - 1, 0), extra)
            takes[index] -= cut
            extra -= cut
            if extra <= 0:
                break
        return [(index, take) for index, take in enumerate(takes) if take > 0]
    need = target - sum(takes)
    while need > 0:
        best = None
        best_left = 0
        for index, frames in enumerate(src):
            left = frames - takes[index]
            if left > best_left:
                best = index
                best_left = left
        if best is None:
            break
        takes[best] += min(best_left, need)
        need -= min(best_left, need)
    pieces = [(index, take) for index, take in enumerate(takes) if take > 0]
    remaining = target - sum(take for _index, take in pieces)
    while remaining > 0:
        for index, frames in enumerate(src):
            if remaining <= 0:
                break
            take = min(frames, cap, remaining)
            pieces.append((index, take))
            remaining -= take
    return pieces


def clip_meta(path):
    _width, _height, frames, audio = probe(path)
    duration = media_duration(path)
    frames_30 = int(round(duration * FPS)) if duration > 0 else frames
    return max(frames_30, 0), audio


def edits_signature(files):
    rows = []
    for path in files:
        stat = path.stat()
        rows.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(rows)


def is_edit_output(path, pattern=None):
    pattern = EDIT_OUTPUT_RE if pattern is None else pattern
    return path.is_file() and pattern.match(path.name) is not None


def cleanup_edit_outputs(keep, pattern=None):
    if not OUT.is_dir():
        return
    pattern = EDIT_OUTPUT_RE if pattern is None else pattern
    keep_resolved = {path.resolve() for path in keep}
    for path in list(OUT.iterdir()):
        if not is_edit_output(path, pattern):
            continue
        if path.name.endswith(".part.mp4") or path.resolve() not in keep_resolved:
            try:
                path.unlink()
            except OSError as exc:
                log(f"не удалился {path.name}: {exc}")
            else:
                if not path.name.endswith(".part.mp4"):
                    log(f"убрал старую итерацию {path.name}")


def plan_stamp_path(name_prefix):
    return STATE / f"edit_plan_{name_prefix}.txt"


def plan_is_stale(name_prefix):
    path = plan_stamp_path(name_prefix)
    if not path.is_file():
        return True
    return path.read_text(encoding="utf-8").strip() != PLAN_VERSION


def mark_plan_current(name_prefix):
    STATE.mkdir(parents=True, exist_ok=True)
    plan_stamp_path(name_prefix).write_text(PLAN_VERSION, encoding="utf-8")


def edit_needs_encode(order, newest, name_prefix="edit"):
    if plan_is_stale(name_prefix):
        return True
    for width, height in SIZES:
        for seconds, _start in VERSIONS:
            dest = edit_output_path(order, width, height, seconds, name_prefix)
            if not dest.is_file() or dest.stat().st_size <= 0:
                return True
            if dest.stat().st_mtime < newest:
                return True
    return False


def exact_video(label_in, frames, label_out):
    # fps в конце иногда съедает последний кадр, и 30 с превращаются в 29.97.
    # Добиваем клон последнего кадра и снова обрезаем ровно до нужного числа.
    return (
        f"{label_in}fps={FPS},trim=end_frame={frames},"
        f"tpad=stop_mode=clone:stop=2,trim=end_frame={frames},"
        f"setpts=PTS-STARTPTS,format=yuv420p{label_out}"
    )


def piece_video(src_label, take, to_white, from_white, width, height, out_label):
    fades = fade_filters(take, to_white, from_white)
    fade = ("," + ",".join(fades)) if fades else ""
    return (
        f"{src_label}fps={FPS},trim=end_frame={take},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=increase:"
        f"force_divisible_by=2:flags=lanczos,crop={width}:{height},"
        f"setsar=1,format=yuv420p{fade}{out_label}"
    )


def append_source_audio(graph, pieces, infos, seconds):
    uses = Counter(index for index, _take in pieces)
    pools = []
    for index, info in enumerate(infos):
        count = uses[index]
        if count <= 0 or not info["audio"]:
            pools.append([])
            continue
        if count == 1:
            pools.append([f"[{index}:a]"])
        else:
            outs = [f"[au{index}_{k}]" for k in range(count)]
            graph.append(f"[{index}:a]asplit={count}{''.join(outs)}")
            pools.append(outs)
    cursors = [0] * len(infos)
    out_labels = []
    for seg, (index, take) in enumerate(pieces):
        dur = take / FPS
        out = f"[sa{seg}]"
        out_labels.append(out)
        if infos[index]["audio"]:
            src = pools[index][cursors[index]]
            cursors[index] += 1
            graph.append(
                f"{src}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS{out}"
            )
        else:
            graph.append(
                f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS{out}"
            )
    joined = "".join(out_labels)
    graph.append(
        f"{joined}concat=n={len(pieces)}:v=0:a=1,apad=whole_dur={seconds},"
        f"atrim=0:{seconds},asetpts=PTS-STARTPTS[bed]"
    )


def build_edit_command(order, infos, pack, pack_audio, music, width, height, seconds, pack_frame, planner=None, name_prefix="edit"):
    if planner is None:
        planner = plan_pieces
    target_frames = seconds * FPS
    pieces = planner([info["frames"] for info in infos], seconds)
    if sum(take for _index, take in pieces) != target_frames:
        raise RuntimeError("не сходится длина итерации")
    graph = []
    uses = Counter(index for index, _take in pieces)
    pools = []
    for index in range(len(infos)):
        count = uses[index]
        if count <= 1:
            pools.append([f"[{index}:v]"] if count == 1 else [])
            continue
        outs = [f"[s{index}_{k}]" for k in range(count)]
        graph.append(f"[{index}:v]split={count}{''.join(outs)}")
        pools.append(outs)
    cursors = [0] * len(infos)
    out_labels = []
    for seg, (index, take) in enumerate(pieces):
        src_label = pools[index][cursors[index]]
        cursors[index] += 1
        is_first = seg == 0
        is_last = seg == len(pieces) - 1
        out = f"[v{seg}]"
        out_labels.append(out)
        graph.append(
            piece_video(
                src_label, take, not is_last, not is_first, width, height, out
            )
        )
    graph.append(
        f"{''.join(out_labels)}concat=n={len(pieces)}:v=1:a=0,"
        f"trim=end_frame={target_frames},setpts=PTS-STARTPTS[base]"
    )
    delay = pack_frame / FPS
    pack_frames = target_frames - pack_frame
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    for path in order:
        cmd += ["-i", str(path)]
    pack_index = len(order)
    if pack is not None:
        cmd += ["-t", f"{pack_frames / FPS + 0.25:.3f}", "-i", str(pack)]
        graph.append(
            f"[{pack_index}:v]trim=end_frame={pack_frames},setpts=PTS-STARTPTS,fps={FPS},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase:"
            f"force_divisible_by=2:flags=lanczos,crop={width}:{height},setsar=1,"
            f"format=rgba,setpts=PTS+{delay}/TB[pk]"
        )
        graph.append(
            "[base][pk]overlay=0:0:eof_action=pass:format=auto[base2]"
        )
        graph.append(exact_video("[base2]", target_frames, "[vout]"))
    else:
        graph.append(exact_video("[base]", target_frames, "[vout]"))
    music_index = None
    if music is not None:
        music_index = pack_index if pack is None else pack_index + 1
        cmd += ["-i", str(music)]
    audio_parts = []
    if music is not None:
        fade_at = 27.0 if seconds <= 30 else 56.0
        fade_for = max(seconds - fade_at, 0.1)
        music_samples = max(48000, int(48000 * (media_duration(music) + 1)))
        graph.append(
            f"[{music_index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"aloop=loop=-1:size={music_samples},atrim=0:{seconds},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={fade_at:.3f}:d={fade_for:.3f}[mus]"
        )
        audio_parts.append("[mus]")
    elif any(info["audio"] for info in infos):
        append_source_audio(graph, pieces, infos, seconds)
        audio_parts.append("[bed]")
    if pack is not None and pack_audio:
        graph.append(
            f"[{pack_index}:a]atrim=0:{pack_frames / FPS:.3f},asetpts=PTS-STARTPTS,"
            f"adelay={int(round(delay * 1000))}|{int(round(delay * 1000))},"
            f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[pa]"
        )
        audio_parts.append("[pa]")
    if len(audio_parts) > 1:
        graph.append(
            f"{''.join(audio_parts)}amix=inputs={len(audio_parts)}:duration=longest:"
            f"dropout_transition=0:normalize=0,atrim=0:{seconds},asetpts=PTS-STARTPTS[aout]"
        )
        audio_label = "[aout]"
    elif audio_parts:
        audio_label = audio_parts[0]
    else:
        audio_label = None
    dest = edit_output_path(order, width, height, seconds, name_prefix)
    tmp = dest.with_name(dest.stem + ".part.mp4")
    cmd += [
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[vout]",
    ]
    if audio_label:
        cmd += ["-map", audio_label, "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-color_range",
        "tv",
        "-movflags",
        "+faststart",
        "-frames:v",
        str(target_frames),
        "-f",
        "mp4",
        str(tmp),
    ]
    return dest, tmp, cmd


def encode_edit(order, infos, pack, music, planner=None, name_prefix="edit"):
    if planner is None:
        planner = plan_pieces
    label = "+".join(path.stem for path in order)
    log(f"итерация {label}")
    if pack is None:
        log("пэкшот не найден, итерация без конечной карточки")
    else:
        log(f"пэкшот {pack.name}")
    if music is None:
        log("музыка не найдена")
    else:
        log(f"музыка {music.name}")
    frame_counts = [info["frames"] for info in infos]
    for seconds, _pack_frame in VERSIONS:
        pieces = planner(frame_counts, seconds)
        shown = " + ".join(f"{order[index].stem} {take / FPS:.1f}с" for index, take in pieces)
        log(f"{seconds}с: {shown}")
    pack_audio = probe(pack)[3] if pack is not None else False
    procs = []
    errors = []
    for width, height in SIZES:
        for seconds, pack_frame in VERSIONS:
            try:
                dest, tmp, cmd = build_edit_command(
                    order, infos, pack, pack_audio, music, width, height, seconds, pack_frame,
                    planner, name_prefix,
                )
            except Exception as exc:
                errors.append(f"{width}x{height} {seconds}с: {exc}")
                continue
            procs.append((dest, tmp, subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )))
    for dest, tmp, proc in procs:
        _stdout, err = proc.communicate()
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            errors.append(f"{dest.name}: {(err or '')[-500:]}")
            continue
        os.replace(tmp, dest)
        log(f"готово {dest.name}")
    if errors:
        raise RuntimeError(" ".join(errors))


def pass_edits(folder=None, name_prefix="edit", pattern=None, planner=None, label="edits", min_clips=0):
    folder = folder or EDITS
    raw = edit_sources(folder)
    if not raw:
        cleanup_edit_outputs(set(), pattern)
        return False
    states = [readiness(path) for path in raw]
    if any(state == "wait" for state in states):
        return True
    if any(state == "skip" for state in states):
        log(f"в {label} есть пустой файл, жду нормальное видео")
        return False
    files, folder_pack = select_edit_clips(raw)
    if len(files) < max(min_clips, 1):
        if min_clips > 1 and len(files) == 1:
            log_once(
                f"{label}-wait",
                f"{label}: жду второй ролик, пока только {files[0].name}",
            )
        else:
            cleanup_edit_outputs(set(), pattern)
        return False
    if folder_pack is not None:
        log_once(
            f"{label}-pack",
            f"пэкшот {folder_pack.name} не ставлю в ряд, в склейке {len(files)}",
        )
    pack = find_packshot(files[0])
    if pack is None:
        pack = folder_pack
    if pack is not None and readiness(pack) == "wait":
        return True
    music = find_music(files[0])
    if music is not None and readiness(music) == "wait":
        return True
    watched = list(files)
    if folder_pack is not None:
        watched.append(folder_pack)
    signature = edits_signature(watched)
    info_by_path = {}
    for path in files:
        frames, audio = clip_meta(path)
        if frames <= 0:
            log(f"не читается {path.name}")
            return False
        info_by_path[path] = {"frames": frames, "audio": audio}
    names = ", ".join(path.name for path in files)
    log_once(f"{label}-clips", f"{label}: {len(files)} роликов ({names})")
    perms = list(permutations(files))
    expected = []
    for perm in perms:
        for width, height in SIZES:
            for seconds, _start in VERSIONS:
                expected.append(edit_output_path(perm, width, height, seconds, name_prefix))
    cleanup_edit_outputs(expected, pattern)
    stamps = [path.stat().st_mtime for path in files]
    if pack is not None:
        stamps.append(pack.stat().st_mtime)
    if music is not None:
        stamps.append(file_stamp(music))
    newest = max(stamps)
    pending = [perm for perm in perms if edit_needs_encode(perm, newest, name_prefix)]
    if not pending:
        mark_plan_current(name_prefix)
        return False
    log(f"{label}: порядков {len(pending)}")
    made = False
    failed = False
    for perm in pending:
        current, current_pack = select_edit_clips(edit_sources(folder))
        current_rows = list(current)
        if current_pack is not None:
            current_rows.append(current_pack)
        if edits_signature(current_rows) != signature:
            log(f"папка {label} изменилась, собираю заново")
            return True
        infos = [info_by_path[path] for path in perm]
        try:
            encode_edit(perm, infos, pack, music, planner, name_prefix)
        except Exception as exc:
            failed = True
            log(f"ошибка {'+'.join(path.stem for path in perm)}: {exc}")
            continue
        made = True
    if not failed:
        mark_plan_current(name_prefix)
    if made:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "Готово: {len(perms)} порядков" with title "Итерации"',
            ],
            check=False,
        )
    return False


def pass_edit_9x16():
    return pass_edits(
        EDIT_9X16,
        "edit_9x16",
        EDIT_9X16_OUTPUT_RE,
        plan_pair_9x16,
        "edit_9x16",
        2,
    )


def encode(src):
    log(f"ресайз {src.name}")
    OUT.mkdir(parents=True, exist_ok=True)
    src_w, src_h, src_frames, src_audio = probe(src)
    if src_w <= 0 or src_frames <= 0:
        raise RuntimeError("не удалось прочитать видео")
    pack = find_packshot(src)
    pack_audio = probe(pack)[3] if pack is not None else False
    music = find_music(src)
    if pack is None:
        log("пэкшот не найден, ресайз без конечной карточки")
    else:
        log(f"пэкшот {pack.name}")
    if music is None:
        log("музыка не найдена")
    else:
        log(f"музыка {music.name}")
    for width, height in SIZES:
        legacy = OUT / f"{src.stem}_{width}x{height}.mp4"
        if legacy.exists():
            legacy.unlink()
    procs = []
    for width, height in SIZES:
        for seconds, pack_frame in VERSIONS:
            dest, tmp, cmd = build_command(
                src, pack, music, src_w, src_h, src_frames, src_audio, pack_audio,
                width, height, seconds, pack_frame,
            )
            procs.append((dest, tmp, subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )))
    errors = []
    for dest, tmp, proc in procs:
        _, err = proc.communicate()
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            errors.append(f"{dest.name}: {(err or '')[-500:]}")
            continue
        os.replace(tmp, dest)
        log(f"готово {dest.name}")
    if errors:
        raise RuntimeError(" ".join(errors))
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "Готово: {src.stem}" with title "Ресайз"',
        ],
        check=False,
    )


def pass_once():
    waiting = False
    for src in sources():
        if not needs_encode(src):
            continue
        pack = find_packshot(src)
        if pack is not None and readiness(pack) == "wait":
            waiting = True
            continue
        music = find_music(src)
        if music is not None and readiness(music) == "wait":
            waiting = True
            continue
        state = readiness(src)
        if state == "wait":
            waiting = True
            continue
        if state == "skip":
            log(f"пропуск пустого файла {src.name}")
            continue
        try:
            encode(src)
        except Exception as exc:
            log(f"ошибка {src.name}: {exc}")
    return waiting


def run(forever):
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        lock_handle = LOCK.open("a")
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("уже работает")
        return 0
    except OSError as exc:
        log(f"не удалось взять lock: {exc}")
        return 1

    log("слежение запущено" if forever else "проверка MAIN, edits и edit_9x16")
    log("edit_9x16: 2 ролика на 30 с — по 15 с, 3 ролика — по 10 с")
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        EDITS.mkdir(parents=True, exist_ok=True)
        EDIT_9X16.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"нет доступа к resize: {exc}")
        if not forever:
            return 1
    try:
        while True:
            try:
                while True:
                    if forever and script_replaced():
                        log("файл скрипта заменили, перезапускаюсь")
                        fcntl.fcntl(lock_handle.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)
                        os.execv(sys.executable, [sys.executable, *sys.argv])
                    waiting_main = pass_once()
                    waiting_edits = pass_edits()
                    waiting_9x16 = pass_edit_9x16()
                    if not (waiting_main or waiting_edits or waiting_9x16):
                        break
                    time.sleep(1)
            except Exception as exc:
                log(f"сбой: {exc}")
                if not forever:
                    return 1
            if not forever:
                break
            time.sleep(1)
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()
    if not forever:
        log("ожидание новых файлов")
    return 0


if __name__ == "__main__":
    sys.exit(run(forever="--daemon" in sys.argv))
