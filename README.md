# vid2obsidian

Convert YouTube videos or raw transcripts into structured Obsidian markdown notes using Claude.

## Setup

```bash
# Install dependencies
uv sync

# Configure
cp .env.example .env
# Edit .env with your API key and vault path
```

**.env**
```
ANTHROPIC_API_KEY=sk-ant-...
OBSIDIAN_VAULT_PATH=~/Documents/ObsidianVault/Videos
```

## Usage

```bash
# From a YouTube URL
uv run main.py --url "https://youtube.com/watch?v=dQw4w9WgXcQ"

# From a transcript file
uv run main.py --transcript ./my-transcript.txt

# From piped stdin
cat transcript.txt | uv run main.py

# Preview without saving
uv run main.py --url "..." --dry-run

# Attach a source URL to a raw transcript
uv run main.py --transcript ./talk.txt --source-url "https://example.com/talk"

# Open in Obsidian after saving
uv run main.py --url "..." --open
```

## Token limit safeguard

Transcripts are checked before hitting the API. The default ceiling is **80,000 estimated tokens** (~2-hour video). If exceeded:

- The tool aborts with the estimated token count and instructions.
- Pass `--force` to truncate the transcript from the end and proceed.
- Pass `--max-tokens N` to set a custom ceiling.

## All flags

| Flag | Description |
|---|---|
| `--url` | YouTube video URL |
| `--transcript` | Path to a `.txt` transcript file |
| `--vault` | Output directory (overrides `OBSIDIAN_VAULT_PATH`) |
| `--source-url` | Manually attach a source URL |
| `--dry-run` | Print note to stdout instead of saving |
| `--force` | Override the token limit (truncates) |
| `--max-tokens N` | Custom token ceiling (default: 80000) |
| `--overwrite` | Replace existing note if filename matches |
| `--open` | Open the note in Obsidian after saving |
| `--model` | Override the Claude model |
| `--verbose` | Print debug info to stderr |
| `--quiet` | Suppress all output except errors |
