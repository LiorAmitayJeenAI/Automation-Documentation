/**
 * The main video section: plays the real Playwright WebM inside a browser-window
 * mockup, overlays a short caption pill, and plays per-step voiceover audio.
 * Frame 0 here = start of the recording (Sequence offset handled by TutorialVideo).
 *
 * Layout (1920×1080):
 *   0–80px      branding bar (JEEN + title)
 *   80–950px    BrowserFrame with the recording + slow camera zoom
 *   950–1040px  caption pill area (gradient background, below the frame)
 *   cross-dissolve overlay on first/last DISSOLVE_FRAMES frames
 */
import React from 'react';
import {
  AbsoluteFill, Audio, OffthreadVideo, Sequence,
  staticFile, useCurrentFrame, interpolate,
} from 'remotion';
import {COLORS, DISSOLVE_FRAMES} from '../constants';
import {SubtitleCue} from '../types';
import {BrowserFrame} from './BrowserFrame';

interface Props {
  recordedVideoFilename: string;
  cues: SubtitleCue[];
  language: 'he' | 'en';
  title: string;
  durationInFrames: number;
}

const TOP_BAR   = 80;   // px reserved above the browser frame for branding
const MARGIN_H  = 70;   // px left/right margin around the browser frame
const FRAME_BTM = 130;  // px reserved below the browser frame for caption pill

/** First sentence of a string, used as caption fallback. */
function firstSentence(text: string): string {
  const m = text.match(/^[^.!?]*[.!?]?/);
  return (m?.[0] ?? text).trim() || text;
}

export const VideoSection: React.FC<Props> = ({
  recordedVideoFilename,
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

  // ── Caption text: prefer explicit caption, else first sentence of narration ──
  const captionText = activeCue
    ? (activeCue.caption?.trim() || firstSentence(activeCue.text))
    : '';

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

  // ── Slow camera zoom: 1.0 → 1.06 across the full clip ──
  const scale = interpolate(frame, [0, durationInFrames], [1.0, 1.06], {
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
        <div style={{
          color: COLORS.accent,
          fontSize: 24,
          fontFamily: 'Arial, sans-serif',
          fontWeight: 700,
          letterSpacing: 3,
        }}>
          JEEN
        </div>
        <div style={{
          color: 'rgba(255,255,255,0.7)',
          fontSize: 18,
          fontFamily: 'Arial, sans-serif',
          direction: isHeb ? 'rtl' : 'ltr',
          maxWidth: 860,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>
          {title}
        </div>
      </div>

      {/* ── Browser frame — leaves FRAME_BTM px at bottom for the caption pill ── */}
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
        <BrowserFrame url="jeenai.app">
          <div style={{
            width: '100%',
            height: '100%',
            transform: `scale(${scale})`,
            transformOrigin: 'center center',
          }}>
            <OffthreadVideo
              src={staticFile(recordedVideoFilename)}
              style={{width: '100%', height: '100%', objectFit: 'cover'}}
            />
          </div>
        </BrowserFrame>
      </div>

      {/* ── Per-step voiceover — unchanged, stays in sync ── */}
      {cues.map((cue, i) =>
        cue.audioFilename ? (
          <Sequence key={`audio-${i}`} from={cue.startFrame}>
            <Audio src={staticFile(cue.audioFilename)} />
          </Sequence>
        ) : null
      )}

      {/* ── Caption pill — centered on the gradient strip below the frame ── */}
      {activeCue && captionText && (
        <div
          style={{
            position: 'absolute',
            bottom: 40,
            left: '50%',
            transform: 'translateX(-50%)',
            maxWidth: '70%',
            padding: '14px 36px',
            background: 'rgba(11,11,26,0.90)',
            borderRadius: 14,
            border: `1px solid rgba(108,92,231,0.35)`,
            boxShadow: '0 8px 32px rgba(0,0,0,0.55)',
            opacity: subtitleOpacity,
            direction: isHeb ? 'rtl' : 'ltr',
            textAlign: isHeb ? 'right' : 'left',
          }}
        >
          <p style={{
            color: COLORS.text,
            fontSize: 28,
            fontFamily: 'Arial, sans-serif',
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
