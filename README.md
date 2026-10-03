# stems2als

`stems2als` is a small cross-platform CLI that creates an Ableton Live Set from
a folder of audio stems. Every MP3, WAV, FLAC, AIFF, OGG, M4A, AAC, or WMA file
becomes a separate Arrangement View audio track starting at bar 1.

The implementation deliberately patches an Ableton-authored template instead
of trying to invent a minimal `.als` document. Ableton's file format is private
and version-specific; using the template installed with Live is both simpler
and more reliable.

## Install

Python 3.10 or newer is required.

```bash
python -m venv .venv
```

macOS/Linux:

```bash
.venv/bin/pip install -e .
```

Windows PowerShell:

```powershell
.venv\Scripts\pip install -e .
```

## Use

```bash
stems2als demo --output demo/demo-set.als --project-name "Demo Set"
```

The command:

- creates one Arrangement View track per audio file;
- copies audio into `Samples/Imported/` beside the set;
- reads each file's real duration and sample rate;
- sets all clips to start together at bar 1;
- finds Ableton Live's installed template automatically on macOS and Windows.

Set a different tempo with `--tempo`:

```bash
stems2als ./my-stems -o ./my-project/my-project.als --tempo 128
```

To generate on another computer or use a specific Live version, pass any
Ableton-authored set containing at least one audio track and one audio clip:

```bash
stems2als ./my-stems -o ./my-project.als --template "/path/to/template.als"
```

You can also set the `ABLETON_TEMPLATE` environment variable.

Run `stems2als --help` for all options.
