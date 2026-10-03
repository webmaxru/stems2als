from __future__ import annotations

import argparse
import copy
import gzip
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from mutagen import File as MutagenFile

from . import __version__

AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".wav",
    ".wma",
}

TRACK_TAGS = {"AudioTrack", "MidiTrack", "GroupTrack"}
GLOBAL_ID_TAGS = {"AutomationTarget", "ModulationTarget", "Pointee"}


@dataclass(frozen=True)
class AudioInfo:
    duration: float
    sample_rate: int

    @property
    def frames(self) -> int:
        return max(1, round(self.duration * self.sample_rate))


@dataclass(frozen=True)
class TemplateFiles:
    project: Path
    clip_source: Path


def _is_audio_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and not path.name.startswith("._")
        and "__MACOSX" not in path.parts
    )


def list_audio_files(directory: Path, recursive: bool = False) -> list[Path]:
    if not directory.exists():
        raise FileNotFoundError(f"Input directory not found: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {directory}")

    candidates = directory.rglob("*") if recursive else directory.iterdir()
    tracks = [path for path in candidates if _is_audio_file(path)]
    if not tracks:
        supported = ", ".join(sorted(AUDIO_EXTENSIONS))
        raise ValueError(
            f"No supported audio files found in {directory}. "
            f"Supported extensions: {supported}"
        )
    return sorted(tracks, key=_track_sort_key)


def is_zip_archive(path: Path) -> bool:
    return path.is_file() and zipfile.is_zipfile(path)


def extract_archive(archive: Path, destination: Path) -> Path:
    if not zipfile.is_zipfile(archive):
        raise ValueError(f"Input file is not a zip archive: {archive}")

    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir():
                continue
            target = (destination / member.filename).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(
                    f"Zip archive contains an unsafe path: {member.filename}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, open(target, "wb") as extracted:
                shutil.copyfileobj(source, extracted)
    return destination


def source_name(input_path: Path) -> str:
    resolved = input_path.expanduser().resolve()
    name = resolved.stem if is_zip_archive(resolved) else resolved.name
    return name or "stems"


def collect_audio_files(input_path: Path, workspace: Path) -> list[Path]:
    input_path = input_path.expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")
    if input_path.is_dir():
        return list_audio_files(input_path)
    if not is_zip_archive(input_path):
        raise ValueError(
            f"Input must be a folder or a zip archive: {input_path}"
        )
    return list_audio_files(extract_archive(input_path, workspace), recursive=True)


def audio_info(path: Path) -> AudioInfo:
    audio = MutagenFile(path)
    if audio is None or getattr(audio, "info", None) is None:
        raise ValueError(f"Could not read audio metadata: {path}")

    duration = float(getattr(audio.info, "length", 0))
    sample_rate = int(getattr(audio.info, "sample_rate", 0))
    if duration <= 0 or sample_rate <= 0:
        raise ValueError(f"Audio file has invalid duration or sample rate: {path}")
    return AudioInfo(duration=duration, sample_rate=sample_rate)


def clean_track_name(path: Path) -> str:
    name = re.sub(r"_\[[^\]]+\]$", "", path.stem)
    stem_marker = re.search(r"_bs\d*stem_mt_\d+_(.+)$", name, flags=re.IGNORECASE)
    if stem_marker:
        name = stem_marker.group(1)
    return name.replace("_", " ").strip() or path.name


def _is_vocal_track(path: Path) -> bool:
    words = re.findall(r"[a-z0-9]+", clean_track_name(path).casefold())
    return any(word in {"vocal", "vocals"} for word in words)


def _is_instrum_track(path: Path) -> bool:
    return clean_track_name(path).casefold() == "instrum"


def _track_sort_key(path: Path) -> tuple[int, str]:
    if _is_vocal_track(path):
        position = 0
    elif _is_instrum_track(path):
        position = 2
    else:
        position = 1
    return position, path.name.casefold()


def _candidate_install_roots() -> Iterable[Path]:
    if sys.platform == "darwin":
        yield from sorted(Path("/Applications").glob("Ableton Live*.app"))
        return

    if os.name == "nt":
        for variable in ("PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(variable)
            if not base:
                continue
            yield from sorted((Path(base) / "Ableton").glob("*Live*"))


def _find_under_install(root: Path, relative_paths: Iterable[Path]) -> Path | None:
    prefixes = (
        root / "Contents" / "App-Resources",
        root / "Resources",
        root,
    )
    for prefix in prefixes:
        for relative_path in relative_paths:
            candidate = prefix / relative_path
            if candidate.is_file():
                return candidate
    return None


def discover_templates(explicit: Path | None = None) -> TemplateFiles:
    if explicit is not None:
        explicit = explicit.expanduser().resolve()
        if not explicit.is_file():
            raise FileNotFoundError(f"Ableton template not found: {explicit}")
        if _find_audio_clip(load_xml(explicit)) is not None:
            return TemplateFiles(explicit, explicit)

        for root in _candidate_install_roots():
            clip_source = _find_clip_source(root)
            if clip_source is not None:
                return TemplateFiles(explicit, clip_source)
        raise ValueError(
            "The provided template has no audio clip to use as a prototype. "
            "Provide an .als file containing at least one audio clip."
        )

    env_template = os.environ.get("ABLETON_TEMPLATE")
    if env_template:
        return discover_templates(Path(env_template))

    for root in reversed(list(_candidate_install_roots())):
        project = _find_under_install(
            root,
            (
                Path("Builtin/Templates/DefaultLiveSet.als"),
                Path("Core Library/Templates/8-Track Template.als"),
            ),
        )
        clip_source = _find_clip_source(root)
        if project is not None and clip_source is not None:
            return TemplateFiles(project, clip_source)

    raise FileNotFoundError(
        "Could not find Ableton's templates. Pass --template /path/to/a/set.als "
        "or set the ABLETON_TEMPLATE environment variable. The template must "
        "contain at least one audio track and one audio clip."
    )


def _find_clip_source(root: Path) -> Path | None:
    return _find_under_install(
        root,
        (
            Path("Core Library/Ableton Folder Info/Sample Reference.als"),
            Path("Core Library/Lessons/Sets/Recording Audio.als"),
        ),
    )


def load_xml(path: Path) -> ET.Element:
    try:
        with gzip.open(path, "rb") as source:
            return ET.fromstring(source.read())
    except (gzip.BadGzipFile, OSError):
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid Ableton template XML: {path}: {exc}") from exc


def _find_audio_clip(root: ET.Element) -> ET.Element | None:
    return root.find(".//AudioClip")


def _set_value(parent: ET.Element, path: str, value: object) -> None:
    element = parent.find(path)
    if element is not None:
        element.set("Value", str(value))


def _clear_clip_slots(track: ET.Element) -> None:
    for value in track.findall(".//ClipSlot/Value"):
        for child in list(value):
            value.remove(child)


def _next_id(root: ET.Element) -> int:
    ids: list[int] = []
    for element in root.iter():
        raw = element.get("Id")
        if raw is not None and raw.isdigit():
            ids.append(int(raw))
    return max(ids, default=0) + 1


def _renumber_track(track: ET.Element, next_id: int) -> int:
    track.set("Id", str(next_id))
    next_id += 1
    for element in track.iter():
        if (
            element.tag in GLOBAL_ID_TAGS or element.tag.endswith("Target")
        ) and "Id" in element.attrib:
            element.set("Id", str(next_id))
            next_id += 1
    return next_id


def _set_track_name(track: ET.Element, name: str) -> None:
    _set_value(track, "./Name/EffectiveName", name)
    _set_value(track, "./Name/UserName", name)
    _set_value(track, "./Name/MemorizedFirstClipName", name)


def _update_file_ref(
    file_ref: ET.Element,
    project_path: Path,
    asset_path: Path,
) -> None:
    relative = Path(os.path.relpath(asset_path, project_path.parent)).as_posix()
    values = {
        "RelativePathType": "1",
        "RelativePath": relative,
        "Path": str(asset_path.resolve()),
        "Type": "1",
        "LivePackName": "",
        "LivePackId": "",
        "OriginalFileSize": str(asset_path.stat().st_size),
        "OriginalCrc": "0",
    }
    for tag, value in values.items():
        element = file_ref.find(tag)
        if element is None:
            element = ET.SubElement(file_ref, tag)
        element.set("Value", value)


def _build_audio_clip(
    prototype: ET.Element,
    source: Path,
    asset_path: Path,
    project_path: Path,
    info: AudioInfo,
    tempo: float,
    clip_id: int,
) -> ET.Element:
    clip = copy.deepcopy(prototype)
    name = clean_track_name(source)
    end_beats = max(info.duration * tempo / 60.0, 1 / 128)

    clip.set("Id", str(clip_id))
    clip.set("Time", "0")
    _set_value(clip, "./CurrentStart", "0")
    _set_value(clip, "./CurrentEnd", f"{end_beats:.9f}")
    _set_value(clip, "./Loop/LoopStart", "0")
    _set_value(clip, "./Loop/LoopEnd", f"{info.duration:.9f}")
    _set_value(clip, "./Loop/StartRelative", "0")
    _set_value(clip, "./Loop/LoopOn", "false")
    _set_value(clip, "./Loop/OutMarker", f"{info.duration:.9f}")
    _set_value(clip, "./Loop/HiddenLoopStart", "0")
    _set_value(clip, "./Loop/HiddenLoopEnd", f"{info.duration:.9f}")
    _set_value(clip, "./Name", name)
    _set_value(clip, "./ScrollerTimePreserver/LeftTime", "0")
    _set_value(clip, "./ScrollerTimePreserver/RightTime", f"{end_beats:.9f}")
    _set_value(clip, "./FreezeStart", "0")
    _set_value(clip, "./FreezeEnd", "0")
    _set_value(clip, "./IsWarped", "false")
    _set_value(clip, "./Sync", "false")
    _set_value(clip, "./Fade", "false")
    _set_value(clip, "./SampleRef/LastModDate", int(source.stat().st_mtime))
    _set_value(clip, "./SampleRef/DefaultDuration", info.frames)
    _set_value(clip, "./SampleRef/DefaultSampleRate", info.sample_rate)

    file_ref = clip.find("./SampleRef/FileRef")
    if file_ref is None:
        sample_ref = clip.find("./SampleRef")
        if sample_ref is None:
            sample_ref = ET.SubElement(clip, "SampleRef")
        file_ref = ET.SubElement(sample_ref, "FileRef")
    _update_file_ref(file_ref, project_path, asset_path)

    source_context = clip.find("./SampleRef/SourceContext")
    if source_context is not None:
        for child in list(source_context):
            source_context.remove(child)

    warp_markers = clip.find("./WarpMarkers")
    if warp_markers is not None:
        for child in list(warp_markers):
            warp_markers.remove(child)
        warp_markers.extend(
            (
                ET.Element(
                    "WarpMarker",
                    {"Id": "0", "SecTime": "0", "BeatTime": "0"},
                ),
                ET.Element(
                    "WarpMarker",
                    {
                        "Id": "1",
                        "SecTime": f"{info.duration:.9f}",
                        "BeatTime": f"{end_beats:.9f}",
                    },
                ),
            )
        )

    return clip


def _set_tempo(root: ET.Element, tempo: float) -> None:
    paths = (
        "./LiveSet/MainTrack/DeviceChain/Mixer/Tempo/Manual",
        "./LiveSet/MasterTrack/DeviceChain/Mixer/Tempo/Manual",
    )
    for path in paths:
        manual = root.find(path)
        if manual is not None:
            manual.set("Value", f"{tempo:.6f}")
            return


def copy_audio_files(audio_files: Iterable[Path], project_path: Path) -> list[Path]:
    assets_dir = project_path.parent / "Samples" / "Imported"
    assets_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    used: set[str] = set()
    for source in audio_files:
        name = source.name
        if name in used:
            stem, suffix = Path(name).stem, Path(name).suffix
            index = 2
            while f"{stem}-{index}{suffix}" in used:
                index += 1
            name = f"{stem}-{index}{suffix}"
        used.add(name)
        destination = assets_dir / name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        copied.append(destination)
    return copied


def build_project(
    template_files: TemplateFiles,
    audio_files: list[Path],
    project_path: Path,
    tempo: float,
) -> ET.Element:
    root = load_xml(template_files.project)
    clip_root = load_xml(template_files.clip_source)
    tracks = root.find("./LiveSet/Tracks")
    track_prototype = root.find("./LiveSet/Tracks/AudioTrack")
    clip_prototype = _find_audio_clip(clip_root)
    if tracks is None or track_prototype is None:
        raise ValueError("Ableton template does not contain an audio track")
    if clip_prototype is None:
        raise ValueError("Ableton template does not contain an audio clip")

    for track in list(tracks):
        if track.tag in TRACK_TAGS:
            tracks.remove(track)

    assets = copy_audio_files(audio_files, project_path)
    next_id = _next_id(root)
    insertion_index = next(
        (index for index, element in enumerate(tracks) if element.tag == "ReturnTrack"),
        len(tracks),
    )

    for source, asset in zip(audio_files, assets):
        track = copy.deepcopy(track_prototype)
        _clear_clip_slots(track)
        next_id = _renumber_track(track, next_id)
        track_name = clean_track_name(source)
        _set_track_name(track, track_name)
        if _is_instrum_track(source):
            _set_value(track, "./DeviceChain/Mixer/Volume/Manual", "0")

        events = track.find(".//MainSequencer/Sample/ArrangerAutomation/Events")
        if events is None:
            raise ValueError(
                "Ableton audio track template has no arrangement event container"
            )
        for child in list(events):
            events.remove(child)
        events.append(
            _build_audio_clip(
                clip_prototype,
                source,
                asset,
                project_path,
                audio_info(source),
                tempo,
                next_id,
            )
        )
        next_id += 1
        tracks.insert(insertion_index, track)
        insertion_index += 1

    next_pointee_id = root.find("./LiveSet/NextPointeeId")
    if next_pointee_id is not None:
        next_pointee_id.set("Value", str(next_id))
    _set_tempo(root, tempo)
    return root


def write_project(root: ET.Element, project_path: Path) -> None:
    project_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="\t")
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    temporary = project_path.with_suffix(project_path.suffix + ".tmp")
    with gzip.open(temporary, "wb", compresslevel=9) as project:
        project.write(xml)
    temporary.replace(project_path)


def run(
    input_path: Path,
    output_path: Path | None,
    project_name: str | None,
    template: Path | None,
    tempo: float,
) -> int:
    if tempo < 20 or tempo > 999:
        raise ValueError("Tempo must be between 20 and 999 BPM")

    input_path = input_path.expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    name = source_name(input_path)
    if output_path is None:
        output_path = Path(name) / f"{name}.als"
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() != ".als":
        output_path = output_path.with_suffix(".als")

    template_files = discover_templates(template)
    with ExitStack() as stack:
        workspace = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        audio_files = collect_audio_files(input_path, workspace)
        root = build_project(template_files, audio_files, output_path, tempo)

    root.set("Creator", f"stems2als {__version__} - {project_name or name}")
    write_project(root, output_path)

    print(f"Created Ableton set: {output_path}")
    print(f"Included tracks: {len(audio_files)}")
    print(f"Audio assets: {output_path.parent / 'Samples' / 'Imported'}")
    print(f"Template: {template_files.project}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create an Ableton Live set from audio stems, with one file per "
            "arrangement track starting at bar 1."
        )
    )
    parser.add_argument(
        "input_path",
        type=Path,
        help="Folder or zip archive containing audio stems",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Output .als path. Defaults to a project folder named after the "
            "input folder or zip archive, e.g. ./my-stems/my-stems.als"
        ),
    )
    parser.add_argument(
        "-n",
        "--project-name",
        help="Optional name recorded in the project's Creator metadata",
    )
    parser.add_argument(
        "-t",
        "--template",
        type=Path,
        help=(
            "Ableton-authored .als template. By default the CLI finds the "
            "installed Ableton Live template on macOS or Windows."
        ),
    )
    parser.add_argument(
        "--tempo",
        type=float,
        default=120.0,
        help="Project tempo in BPM (default: 120)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(
            args.input_path,
            args.output,
            args.project_name,
            args.template,
            args.tempo,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
