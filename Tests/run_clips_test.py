"""
Standalone test runner for the Clips-style smooth recorder.

Quick smoke test (built-in 1-step script, no TTS or Remotion):
    python -m backend.run_clips_test --headless

Full pipeline from a Confluence page (TTS + Clips record + Remotion render):
    python -m backend.run_clips_test --headless \
        --confluence-url "https://jeenai.atlassian.net/wiki/spaces/GKC/pages/383615020/Part+3+-+Side+Menu"

Flags:
    --headless           run new-headless (CDP fallback, most reliable)
    --admin              record the admin app
    --language he|en     login language
    --mechanism auto|mediarecorder|cdp
    --confluence-url URL fetch page -> LLM script -> TTS -> record -> Remotion MP4
    --no-render          skip the Remotion render (output raw webm + JSON only)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re

from backend.config import REGULAR_URL, ADMIN_URL
from Tests.recorder_clips import record_clips_video


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("clips_test")


def _build_test_script(base_url: str) -> list[dict]:
    return [
        {
            "url": base_url,
            "action": "home — land and settle",
            "settle_ms": 2500,
            "interactions": [
                {"type": "wait", "ms": 800},
            ],
        },
    ]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Clips-style smooth recorder test run")
    p.add_argument("--headless", action="store_true", help="run new-headless instead of headed")
    p.add_argument("--admin", action="store_true", help="record the admin app instead of the regular app")
    p.add_argument("--language", default="he", help="login language (he/en)")
    p.add_argument(
        "--mechanism",
        default="auto",
        choices=["auto", "mediarecorder", "cdp"],
        help="capture mechanism (auto tries MediaRecorder then falls back to CDP screencast)",
    )
    p.add_argument(
        "--confluence-url",
        default="",
        help="Confluence page URL — enables full pipeline (fetch -> LLM script -> TTS -> record -> Remotion)",
    )
    p.add_argument(
        "--no-render",
        action="store_true",
        help="skip Remotion render; output raw webm + execution_log.json only",
    )
    return p.parse_args()


async def _main() -> None:
    args = _parse_args()
    link_type = "admin" if args.admin else "regular"
    base_url = ADMIN_URL if args.admin else REGULAR_URL

    title = "Clips Test"
    audio_results = None

    if args.confluence_url:
        # ── Full pipeline: Confluence -> LLM -> TTS -> record -> render ──
        from backend.services import confluence, llm, tts

        print("=== 1. Fetching Confluence page ===")
        title, md = await confluence.fetch_page_as_markdown(args.confluence_url)
        print(f"Fetched: {title} ({len(md)} chars)")

        print("\n=== 2. Generating video script via LLM ===")
        script = await llm.generate_video_script(
            md, language=args.language, base_url=base_url, link_type=link_type,
        )
        print(f"Script steps: {len(script)}")
        for i, s in enumerate(script):
            print(f"  step {i+1}: {s.get('action','?')}")

        print("\n=== 3. Synthesizing TTS audio ===")
        session_id = "clips-" + re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:40].lower()
        audio_results = await tts.synthesize_script(
            script, session_id, language=args.language,
        )
        n_audio = sum(1 for a in audio_results if a)
        print(f"Audio ready: {n_audio}/{len(script)} steps voiced")
    else:
        script = _build_test_script(base_url)
        session_id = "clips-test"

    logger.info(
        "Starting Clips recording: %s app, %d steps, headless=%s, mechanism=%s",
        link_type, len(script), args.headless, args.mechanism,
    )

    print(f"\n=== {'4' if args.confluence_url else '1'}. Recording with Clips recorder ===")
    result = await record_clips_video(
        video_script=script,
        base_url=base_url,
        link_type=link_type,
        session_id=session_id,
        language=args.language,
        headless=args.headless,
        mechanism=args.mechanism,
        audio_results=audio_results,
    )

    print(f"\n--- Recording complete ---")
    print(f"mechanism     : {result['mechanism']}")
    print(f"video (.webm) : {result['webm_path']}")
    print(f"metadata JSON : {result['metadata_path']}")
    print(f"fps           : {result['fps']}")
    print(f"duration      : {result['total_seconds']:.1f}s")
    print(f"recorded steps: {len(result['recorded_steps'])}  failed: {len(result['failed_steps'])}")

    # ── Remotion render (only for full-pipeline runs with audio) ──
    if args.confluence_url and not args.no_render:
        from backend.services import video

        print(f"\n=== 5. Rendering with Remotion (jump-cuts + subtitles + audio) ===")
        mp4_path = await video.render_video(
            title=title,
            recording_result=result,
            audio_results=audio_results,
            language=args.language,
            session_id=session_id,
            file_stem=re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:60],
        )
        if mp4_path:
            print(f"\n=== DONE ===")
            print(f"MP4: {mp4_path}")
        else:
            print("\nRemotionrender returned no output (check logs above)")
    else:
        print(f"\n=== DONE ===")
        print(f"webm: {result['webm_path']}")
        print(f"json: {result['metadata_path']}")


if __name__ == "__main__":
    asyncio.run(_main())
