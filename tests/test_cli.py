from __future__ import annotations

import gzip
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

from stems2als.cli import (
    TemplateFiles,
    build_project,
    clean_track_name,
    discover_templates,
    list_audio_files,
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


if __name__ == "__main__":
    unittest.main()
