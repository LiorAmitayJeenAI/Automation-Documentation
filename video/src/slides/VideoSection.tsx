/**
 * The main video section: plays jump-cut segments from the Playwright WebM,
 * skipping loading/navigation time. Overlays caption pill and per-step audio.
 * Frame 0 here = start of the output timeline (Sequence offset handled by TutorialVideo).
 *
 * Layout (1920×1080):
 *   0–80px      branding bar (JEEN + title)
 *   80–950px    recording with slow camera zoom
 *   950–1040px  caption pill area (gradient background, below the frame)
 *   cross-dissolve overlay on first/last DISSOLVE_FRAMES frames
 */
import React from 'react';
import {
  AbsoluteFill, Audio, Img, OffthreadVideo, Sequence,
  staticFile, useCurrentFrame, interpolate,
} from 'remotion';
import {COLORS, DISSOLVE_FRAMES} from '../constants';
import {FONT_FAMILY} from '../fonts';
import {SubtitleCue, VideoSegment} from '../types';

interface Props {
  recordedVideoFilename: string;
  segments: VideoSegment[];
  cues: SubtitleCue[];
  language: 'he' | 'en';
  title: string;
  durationInFrames: number;
}

const TOP_BAR   = 80;   // px reserved above the browser frame for branding
const MARGIN_H  = 70;   // px left/right margin around the browser frame
const FRAME_BTM = 130;  // px reserved below the browser frame for caption pill


export const VideoSection: React.FC<Props> = ({
  recordedVideoFilename,
  segments,
  cues,
  language,
  title,
  durationInFrames,
}) => {
  const frame = useCurrentFrame();
  const isHeb = language === 'he';

  // ── Active cue (track index for next-cue fade-out timing) ──
  let activeCueIndex = -1;
  let activeCue: SubtitleCue | null = null;
  for (let i = cues.length - 1; i >= 0; i--) {
    if (frame >= cues[i].startFrame) {
      activeCue = cues[i];
      activeCueIndex = i;
      break;
    }
  }

  // ── Subtitle text: show the full narration (matches the spoken voiceover) ──
  const captionText = activeCue?.text ?? '';

  // ── Subtitle opacity: fade IN over 15 frames, fade OUT 15 frames before next cue ──
  const nextCue = activeCueIndex >= 0 && activeCueIndex < cues.length - 1
    ? cues[activeCueIndex + 1]
    : null;
  const cueStart    = activeCue ? activeCue.startFrame : 0;
  const cueEnd      = nextCue ? nextCue.startFrame : durationInFrames;
  const fadeInEnd   = cueStart + 15;
  const fadeOutStart = Math.max(fadeInEnd + 10, cueEnd - 15);
  const subtitleOpacity = activeCue
    ? interpolate(
        frame,
        [cueStart, fadeInEnd, fadeOutStart, cueEnd],
        [0, 1, 1, 0],
        {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
      )
    : 0;

  // ── Slow camera zoom: 1.0 → 1.04 across the full clip ──
  const scale = interpolate(frame, [0, durationInFrames], [1.0, 1.04], {
    extrapolateRight: 'clamp',
  });

  // ── Cross-dissolve overlay: fades in at start and out at end of segment ──
  const dissolveIn  = interpolate(frame, [0, DISSOLVE_FRAMES], [1, 0], {extrapolateRight: 'clamp'});
  const dissolveOut = interpolate(
    frame,
    [durationInFrames - DISSOLVE_FRAMES, durationInFrames],
    [0, 1],
    {extrapolateLeft: 'clamp'},
  );
  const dissolveOpacity = Math.max(dissolveIn, dissolveOut);

  const videoSrc = staticFile(recordedVideoFilename);

  return (
    <AbsoluteFill style={{background: COLORS.bgGradient}}>

      {/* ── Branding bar — on background, above the browser frame ── */}
      <div
        style={{
          position: 'absolute',
          top: 0, left: 0, right: 0,
          height: TOP_BAR,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0 80px',
          zIndex: 10,
        }}
      >
        <Img
          src={staticFile('jeen-logo.png')}
          style={{width: 100, height: 'auto'}}
        />
        <div style={{
          color: COLORS.textMuted,
          fontSize: 18,
          fontFamily: FONT_FAMILY,
          fontWeight: 500,
          direction: isHeb ? 'rtl' : 'ltr',
          maxWidth: 860,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>
          {title}
        </div>
      </div>

      {/* ── Video segments (jump-cuts) — only visible-content portions ── */}
      <div
        style={{
          position: 'absolute',
          top: TOP_BAR,
          left: MARGIN_H,
          right: MARGIN_H,
          bottom: FRAME_BTM,
          borderRadius: 16,
          overflow: 'hidden',
          boxShadow: '0 40px 120px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{
          width: '100%',
          height: '100%',
          transform: `scale(${scale})`,
          transformOrigin: 'center center',
        }}>
          {segments.map((seg, i) => (
            <Sequence
              key={`seg-${i}`}
              from={seg.outputStartFrame}
              durationInFrames={seg.durationFrames}
            >
              <Sequence from={-seg.sourceStartFrame}>
                <OffthreadVideo
                  src={videoSrc}
                  style={{width: '100%', height: '100%', objectFit: 'contain'}}
                />
              </Sequence>
            </Sequence>
          ))}
        </div>
      </div>

      {/* ── Per-step voiceover ── */}
      {cues.map((cue, i) =>
        cue.audioFilename ? (
          <Sequence key={`audio-${i}`} from={cue.startFrame}>
            <Audio src={staticFile(cue.audioFilename)} />
          </Sequence>
        ) : null
      )}

      {/* ── Caption pill — centered below the frame ── */}
      {activeCue && captionText && (
        <div
          style={{
            position: 'absolute',
            bottom: 40,
            left: '50%',
            transform: 'translateX(-50%)',
            maxWidth: '70%',
            padding: '14px 36px',
            background: COLORS.narrationBg,
            borderRadius: 14,
            border: `1px solid ${COLORS.border}`,
            boxShadow: '0 8px 32px rgba(0,0,0,0.3)',
            opacity: subtitleOpacity,
            direction: isHeb ? 'rtl' : 'ltr',
            textAlign: isHeb ? 'right' : 'left',
          }}
        >
          <p style={{
            color: COLORS.textLight,
            fontSize: 28,
            fontFamily: FONT_FAMILY,
            fontWeight: 500,
            margin: 0,
            lineHeight: 1.45,
            whiteSpace: 'normal',
            wordBreak: 'break-word',
          }}>
            {captionText}
          </p>
        </div>
      )}

      {/* ── Cross-dissolve overlay — branded background fades in/out at edges ── */}
      {dissolveOpacity > 0 && (
        <AbsoluteFill
          style={{
            background: COLORS.bgGradient,
            opacity: dissolveOpacity,
            pointerEvents: 'none',
          }}
        />
      )}

    </AbsoluteFill>
  );
};
