"""vid2obsidian — Convert video content into Obsidian markdown notes."""

import argparse
import hashlib
import os
import re
import shutil
import sys
import time
import webbrowser
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TRANSCRIPTS_DIR = SCRIPT_DIR / "transcripts"
TRANSCRIPTS_ARCHIVE_DIR = TRANSCRIPTS_DIR / "archived"

import anthropic
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

load_dotenv()

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 80_000
MAX_RETRIES = 3

SYSTEM_PROMPT = """\
You are a precise note-taking assistant for an Obsidian knowledge base focused on tech content. Given a video transcript, generate a concise Obsidian-flavored Markdown note. Structure it as:

- A YAML frontmatter block with:
  - tags: 2–4 lowercase tags, specific (e.g. llm, devops, rust, system-design)
  - source: the URL (if provided; omit the field entirely if not available)
  - date_created: today's date in YYYY-MM-DD format
  - topics: a flat list of 3–5 core concepts this video covers

- A H1 title (infer from the transcript content)
- A one-sentence TL;DR in a blockquote
- ## Key Takeaways — 3–6 bullet points, dense and specific, no fluff
- ## Technologies & Tools — only if explicitly mentioned, formatted as inline code (e.g. `Kubernetes`, `PyTorch`). Omit this section entirely if none are mentioned.
- ## Concepts — bullet list of the main ideas covered, each as an Obsidian wikilink (e.g. [[Retrieval Augmented Generation]], [[Kubernetes Operators]]). Use the canonical, widely recognized name so links match across notes.
- ## Why It Matters — 2–3 sentences max
- ## Related — leave as an empty bullet list placeholder

Wikilink rules:
- Only wikilink concepts substantial enough to appear across multiple videos (e.g. [[Transformers]], [[CI/CD]], [[Vector Database]])
- Never wikilink generic words like [[video]] or [[code]]
- Always use the same canonical form (e.g. always [[Large Language Model]], never [[LLM]] or [[large language models]])
- Aim for 4–8 wikilinks per note

If the video centers around a specific framework, methodology, or multi-part system (e.g. "6 pillars of...", "3 phases of...", "the DORA metrics"), add a dedicated section with a descriptive heading (e.g. ## The Six Pillars of the Well-Architected Framework). Present each component as a sub-item with a brief explanation. Place this section after ## Key Takeaways. This is more important than keeping the note short — structure is signal.

Keep everything tight. Avoid restating the obvious. Prioritize actionable or surprising insights.\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def eprint(*args, quiet=False, **kwargs):
    if not quiet:
        print(*args, file=sys.stderr, **kwargs)


def slugify(text: str, max_len: int = 80) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:max_len]


def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:embed/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def estimate_tokens(text: str) -> int:
    return len(text) // 4


# ---------------------------------------------------------------------------
# Transcript fetching
# ---------------------------------------------------------------------------

def fetch_youtube_transcript(video_id: str) -> str:
    transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

    # Prefer manual captions, fallback to generated, prefer English
    transcript = None
    try:
        transcript = transcript_list.find_manually_created_transcript(["en"])
    except NoTranscriptFound:
        pass

    if transcript is None:
        try:
            transcript = transcript_list.find_generated_transcript(["en"])
        except NoTranscriptFound:
            pass

    if transcript is None:
        # Take whatever is first
        for t in transcript_list:
            transcript = t
            break

    if transcript is None:
        raise RuntimeError("No transcript available for this video.")

    entries = transcript.fetch()
    return " ".join(e.text for e in entries)


# ---------------------------------------------------------------------------
# API call with retry
# ---------------------------------------------------------------------------

def call_api(client: anthropic.Anthropic, transcript: str, source_url: str | None,
             model: str, quiet: bool) -> tuple[str, anthropic.types.Usage]:
    user_content = f"Source URL: {source_url}\n\n" if source_url else ""
    user_content += f"Transcript:\n\n{transcript}"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            return response.content[0].text, response.usage
        except anthropic.RateLimitError as e:
            last_error = e
            wait = 2 ** attempt
            eprint(f"Rate limited. Retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})...", quiet=quiet)
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                wait = 2 ** attempt
                eprint(f"Server error {e.status_code}. Retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})...", quiet=quiet)
                time.sleep(wait)
            else:
                raise

    raise last_error


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="vid2obsidian",
        description="Convert video content into Obsidian markdown notes.",
    )
    parser.add_argument("--url", help="YouTube video URL")
    parser.add_argument("--transcript", help="Path to a .txt transcript file")
    parser.add_argument("--vault", help="Output directory (overrides OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--source-url", dest="source_url", help="Manually attach a source URL")
    parser.add_argument("--dry-run", action="store_true", help="Print note to stdout instead of saving")
    parser.add_argument("--force", action="store_true", help="Override the transcript token limit (truncates)")
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help=f"Custom token ceiling (default: {DEFAULT_MAX_TOKENS})")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing note if filename matches")
    parser.add_argument("--open", action="store_true", dest="open_in_obsidian",
                        help="Open the note in Obsidian after saving")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default: {DEFAULT_MODEL})")
    parser.add_argument("--verbose", action="store_true", help="Print debug info to stderr")
    parser.add_argument("--quiet", action="store_true", help="Suppress all output except errors")
    args = parser.parse_args()

    quiet = args.quiet
    verbose = args.verbose and not quiet

    # --- Determine input mode ---
    transcript_text = None
    video_id = None
    source_url = args.source_url

    transcript_source_path: Path | None = None  # set when using --transcript, for archiving

    if args.url:
        video_id = extract_video_id(args.url)
        if not video_id:
            print(f"ERROR: Could not parse a video ID from URL: {args.url}", file=sys.stderr)
            sys.exit(1)
        if not source_url:
            source_url = args.url
        eprint("Fetching YouTube transcript...", quiet=quiet)
        try:
            transcript_text = fetch_youtube_transcript(video_id)
        except (TranscriptsDisabled, NoTranscriptFound, RuntimeError) as e:
            print(
                f"ERROR: Could not fetch transcript for {args.url}.\n"
                f"  Reason: {e}\n"
                "  Try downloading the transcript manually and using --transcript instead.\n"
                "  Note: auto-generated captions may be disabled for this video.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Save fetched transcript to transcripts/
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        saved_transcript_path = TRANSCRIPTS_DIR / f"{video_id}.txt"
        saved_transcript_path.write_text(transcript_text, encoding="utf-8")
        eprint(f"Transcript saved: {saved_transcript_path}", quiet=quiet)

    elif args.transcript:
        # Resolve relative paths against the transcripts/ folder
        path = Path(args.transcript)
        if not path.is_absolute():
            path = TRANSCRIPTS_DIR / path
        if not path.exists():
            print(f"ERROR: Transcript file not found: {path}", file=sys.stderr)
            sys.exit(1)
        transcript_text = path.read_text(encoding="utf-8")
        transcript_source_path = path

    elif not sys.stdin.isatty():
        transcript_text = sys.stdin.read()
        if not transcript_text.strip():
            print("ERROR: Piped stdin is empty.", file=sys.stderr)
            parser.print_help(sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(0)

    # --- Token safeguard ---
    estimated_tokens = estimate_tokens(transcript_text)
    if verbose:
        eprint(f"Transcript length: {len(transcript_text)} chars (~{estimated_tokens} tokens)")

    if estimated_tokens > args.max_tokens:
        if not args.force:
            print(
                f"ERROR: Transcript is too long (~{estimated_tokens:,} estimated tokens, limit is {args.max_tokens:,}).\n"
                f"  Use --force to process anyway (transcript will be truncated from the end).\n"
                f"  Use --max-tokens N to set a custom ceiling.",
                file=sys.stderr,
            )
            sys.exit(1)
        char_limit = args.max_tokens * 4
        transcript_text = transcript_text[:char_limit]
        eprint(
            f"WARNING: Transcript truncated to ~{args.max_tokens:,} tokens. "
            "Note will be based on partial content.",
            quiet=quiet,
        )

    # --- API key ---
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "  Set it in a .env file or as an environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Vault path ---
    vault_path_str = args.vault or os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault_path_str and not args.dry_run:
        print(
            "ERROR: No vault path configured.\n"
            "  Use --vault or set OBSIDIAN_VAULT_PATH in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    vault = Path(vault_path_str).expanduser() if vault_path_str else None

    if vault and not args.dry_run:
        try:
            vault.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"ERROR: Cannot create vault directory {vault.resolve()}: {e}", file=sys.stderr)
            sys.exit(1)

    # --- API call ---
    client = anthropic.Anthropic(api_key=api_key)
    eprint("Generating note...", quiet=quiet)
    t0 = time.time()

    slug_for_failure = slugify(video_id or "transcript")
    try:
        note_text, usage = call_api(client, transcript_text, source_url, args.model, quiet)
    except Exception as e:
        print(f"ERROR: API call failed after {MAX_RETRIES} retries: {e}", file=sys.stderr)
        if vault:
            failed_dir = vault / "_failed"
            failed_dir.mkdir(parents=True, exist_ok=True)
            failed_path = failed_dir / f"{slug_for_failure}.txt"
            failed_path.write_text(transcript_text, encoding="utf-8")
            print(f"  Raw transcript saved to: {failed_path}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - t0

    # Cost estimate (claude-sonnet-4 pricing: $3/M input, $15/M output — approximate)
    input_cost = usage.input_tokens / 1_000_000 * 3.0
    output_cost = usage.output_tokens / 1_000_000 * 15.0
    eprint(
        f"Tokens: {usage.input_tokens} in / {usage.output_tokens} out | "
        f"Est. cost: ${input_cost + output_cost:.4f} | {elapsed:.1f}s",
        quiet=quiet,
    )

    # --- Infer slug from note title ---
    title_match = re.search(r"^#\s+(.+)$", note_text, re.MULTILINE)
    title_slug = slugify(title_match.group(1) if title_match else "note", max_len=60)

    if video_id:
        filename = f"{title_slug}-{video_id}.md"
    else:
        content_hash = hashlib.sha256(transcript_text.encode()).hexdigest()[:8]
        filename = f"{title_slug}-{content_hash}.md"

    # --- Dry run ---
    if args.dry_run:
        print(note_text)
        return

    # --- Save ---
    output_path = vault / filename
    if output_path.exists() and not args.overwrite:
        eprint(f"Note already exists: {output_path}\n  Pass --overwrite to replace it.", quiet=quiet)
        sys.exit(0)

    output_path.write_text(note_text, encoding="utf-8")
    eprint(f"Saved: {output_path}", quiet=quiet)

    # Archive the source transcript after successful save
    if transcript_source_path:
        TRANSCRIPTS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archive_path = TRANSCRIPTS_ARCHIVE_DIR / transcript_source_path.name
        shutil.move(str(transcript_source_path), archive_path)
        eprint(f"Transcript archived: {archive_path}", quiet=quiet)

    if verbose:
        eprint(f"Filename: {filename}")

    # --- Open in Obsidian ---
    if args.open_in_obsidian:
        vault_name = vault.name
        note_name = filename[:-3]  # strip .md
        uri = f"obsidian://open?vault={vault_name}&file={note_name}"
        webbrowser.open(uri)


if __name__ == "__main__":
    main()
