from __future__ import annotations

import gzip
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from stems2als.cli import (
    TemplateFiles,
    build_project,
    clean_track_name,
    collect_audio_files,
    discover_templates,
    extract_archive,
    is_zip_archive,
    list_audio_files,
    source_name,
    write_project,
)


class Stems2AlsTests(unittest.TestCase):
    @staticmethod
    def _write_wav(path: Path, sample_rate: int = 8000) -> None:
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(b"\0\0" * sample_rate)

    def test_clean_track_name(self) -> None:
        self.assertEqual(clean_track_name(Path("lead_vocals.flac")), "lead vocals")
        self.assertEqual(
            clean_track_name(
                Path("20261002-10._bs6stem_mt_0_vocals_[mvsep.com].mp3")
            ),
            "vocals",
        )

    def test_list_audio_files_is_sorted_and_filters_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "B.WAV").touch()
            (folder / "a.mp3").touch()
            (folder / "instrum.flac").touch()
            (folder / "notes.txt").touch()
            (folder / "vocals.ogg").touch()
            self.assertEqual(
                [path.name for path in list_audio_files(folder)],
                ["vocals.ogg", "a.mp3", "B.WAV", "instrum.flac"],
            )

    def test_installed_template_can_build_project(self) -> None:
        try:
            templates = discover_templates()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            stems = temporary_path / "stems"
            stems.mkdir()
            self._write_wav(stems / "bass.wav")
            self._write_wav(
                stems / "20261002-10._bs6stem_mt_0_instrum_[mvsep.com].wav"
            )
            self._write_wav(
                stems / "20261002-10._bs6stem_mt_0_vocals_[mvsep.com].wav"
            )
            audio_files = list_audio_files(stems)
            output = temporary_path / "demo.als"
            root = build_project(templates, audio_files, output, 120)
            write_project(root, output)

            with gzip.open(output, "rb") as project:
                parsed = ET.fromstring(project.read())

            tracks = parsed.findall("./LiveSet/Tracks/AudioTrack")
            clips = parsed.findall(
                "./LiveSet/Tracks/AudioTrack/DeviceChain/MainSequencer/"
                "Sample/ArrangerAutomation/Events/AudioClip"
            )
            self.assertEqual(len(tracks), len(audio_files))
            self.assertEqual(len(clips), len(audio_files))
            self.assertEqual(
                [
                    track.find("./Name/EffectiveName").get("Value")
                    for track in tracks
                ],
                ["vocals", "bass", "instrum"],
            )
            self.assertEqual(
                tracks[-1]
                .find("./DeviceChain/Mixer/Volume/Manual")
                .get("Value"),
                "0",
            )

            referenced = {
                element.get("Value")
                for element in parsed.findall(".//AudioClip/SampleRef/FileRef/RelativePath")
            }
            expected = {
                f"Samples/Imported/{audio_file.name}" for audio_file in audio_files
            }
            self.assertEqual(referenced, expected)
            self.assertTrue(all(clip.get("Time") == "0" for clip in clips))
            self.assertTrue(all(clip.findall("./WarpMarkers/WarpMarker") for clip in clips))
            self.assertTrue(
                all(
                    element.get("Value") == "1"
                    for element in parsed.findall(
                        ".//AudioClip/SampleRef/FileRef/RelativePathType"
                    )
                )
            )

            pointee_ids = [
                element.get("Id")
                for element in parsed.iter()
                if (
                    element.tag == "Pointee" or element.tag.endswith("Target")
                ) and element.get("Id") is not None
            ]
            self.assertEqual(len(pointee_ids), len(set(pointee_ids)))

    def test_template_files_dataclass(self) -> None:
        path = Path("template.als")
        self.assertEqual(TemplateFiles(path, path).project, path)

    def test_source_name_uses_folder_or_archive_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "My Stems"
            folder.mkdir()
            self._write_wav(folder / "vocals.wav")
            archive = Path(temporary) / "My Archive.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.write(folder / "vocals.wav", "vocals.wav")

            self.assertEqual(source_name(folder), "My Stems")
            self.assertEqual(source_name(archive), "My Archive")
            self.assertTrue(is_zip_archive(archive))
            self.assertFalse(is_zip_archive(folder))

    def test_collect_audio_files_reads_nested_zip_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            stems = temporary_path / "stems"
            stems.mkdir()
            self._write_wav(stems / "bass.wav")
            self._write_wav(stems / "instrum.wav")
            self._write_wav(stems / "vocals.wav")

            archive = temporary_path / "bundle.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for audio_file in sorted(stems.iterdir()):
                    bundle.write(audio_file, f"bundle/{audio_file.name}")
                bundle.writestr("bundle/notes.txt", "ignored")
                bundle.writestr("__MACOSX/bundle/._vocals.wav", "resource fork")

            workspace = temporary_path / "workspace"
            workspace.mkdir()
            collected = collect_audio_files(archive, workspace)
            self.assertEqual(
                [path.name for path in collected],
                ["vocals.wav", "bass.wav", "instrum.wav"],
            )

    def test_collect_audio_files_rejects_unsupported_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            plain_file = temporary_path / "notes.txt"
            plain_file.write_text("not an archive")
            with self.assertRaises(ValueError):
                collect_audio_files(plain_file, temporary_path / "workspace")
            with self.assertRaises(FileNotFoundError):
                collect_audio_files(temporary_path / "missing", temporary_path)

    def test_extract_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            archive = temporary_path / "evil.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escaped.wav", "payload")

            destination = temporary_path / "workspace"
            destination.mkdir()
            with self.assertRaises(ValueError):
                extract_archive(archive, destination)
            self.assertFalse((temporary_path / "escaped.wav").exists())

    def test_zip_archive_builds_same_project_as_folder(self) -> None:
        try:
            templates = discover_templates()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            stems = temporary_path / "stems"
            stems.mkdir()
            self._write_wav(stems / "bass.wav")
            self._write_wav(stems / "instrum.wav")
            self._write_wav(stems / "vocals.wav")

            archive = temporary_path / "bundle.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for audio_file in sorted(stems.iterdir()):
                    bundle.write(audio_file, audio_file.name)

            workspace = temporary_path / "workspace"
            workspace.mkdir()
            audio_files = collect_audio_files(archive, workspace)
            output = temporary_path / "bundle" / "bundle.als"
            root = build_project(templates, audio_files, output, 120)
            write_project(root, output)

            with gzip.open(output, "rb") as project:
                parsed = ET.fromstring(project.read())

            tracks = parsed.findall("./LiveSet/Tracks/AudioTrack")
            self.assertEqual(
                [
                    track.find("./Name/EffectiveName").get("Value")
                    for track in tracks
                ],
                ["vocals", "bass", "instrum"],
            )
            self.assertEqual(
                tracks[-1].find("./DeviceChain/Mixer/Volume/Manual").get("Value"),
                "0",
            )
            for audio_file in audio_files:
                self.assertTrue(
                    (output.parent / "Samples" / "Imported" / audio_file.name).is_file()
                )


if __name__ == "__main__":
    unittest.main()
