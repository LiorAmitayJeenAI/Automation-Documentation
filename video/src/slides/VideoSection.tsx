/**
 * The main video section: plays jump-cut segments from the Playwright WebM,
 * skipping loading/navigation time. Overlays caption pill and per-step audio.
 * Frame 0 here = start of the output timeline (Sequence offset handled by TutorialVideo).
 *
 * Layout (1920×1080):
 *   Full-frame gradient background (pink → red-orange → golden).
 *   Top bar (56px): logo + black title text over the gradient.
 *   Recording area fills remaining space with 6px gradient margin on sides/bottom.
 *   Caption pill overlays the bottom of the recording.
 *   Cross-dissolve overlay on first/last DISSOLVE_FRAMES frames.
 */
import React from 'react';
import {
  AbsoluteFill, Audio, Img, OffthreadVideo, Sequence,
  staticFile, useCurrentFrame, interpolate,
} from 'remotion';
import {COLORS, DISSOLVE_FRAMES} from '../constants';
import {FONT_FAMILY} from '../fonts';
import {SubtitleCue, VideoSegment} from '../types';

const TOP_BAR_H = 56;
const BORDER_W = 6;

interface Props {
  recordedVideoFilename: string;
  segments: VideoSegment[];
  cues: SubtitleCue[];
  language: 'he' | 'en';
  title: string;
  durationInFrames: number;
}


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
    <AbsoluteFill style={{background: COLORS.bgGradient, flexDirection: 'column', display: 'flex'}}>

      {/* ── Top bar: logo + title (gradient background inherited from parent) ── */}
      <div
        style={{
          height: TOP_BAR_H,
          display: 'flex',
          alignItems: 'center',
          padding: '0 28px',
          flexShrink: 0,
          overflow: 'visible',
          zIndex: 5,
        }}
      >
        <Img
          src={staticFile('jeen-logo.png')}
          style={{height: 80, width: 'auto', flexShrink: 0}}
        />
        <div
          style={{
            flex: 1,
            textAlign: 'center',
            color: '#000000',
            fontSize: 26,
            fontFamily: FONT_FAMILY,
            fontWeight: 500,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            padding: '0 20px',
          }}
        >
          {title}
        </div>
        <div style={{width: 100, flexShrink: 0}} />
      </div>

      {/* ── Recording area (gradient border visible as padding around it) ── */}
      <div
        style={{
          flex: 1,
          marginLeft: BORDER_W,
          marginRight: BORDER_W,
          marginBottom: BORDER_W,
          position: 'relative',
          overflow: 'hidden',
          borderRadius: 4,
          minHeight: 0,
        }}
      >

        {/* ── Video segments (jump-cuts) ── */}
        {segments.map((seg, i) => (
          <Sequence
            key={`seg-${i}`}
            from={seg.outputStartFrame}
            durationInFrames={seg.durationFrames}
          >
            <Sequence from={-seg.sourceStartFrame}>
              <OffthreadVideo
                src={videoSrc}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  height: '100%',
                  objectFit: 'contain',
                  background: COLORS.bgGradient,
                }}
              />
            </Sequence>
          </Sequence>
        ))}

        {/* ── Caption pill — overlaid at bottom of recording area ── */}
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
              zIndex: 10,
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

        {/* ── Cross-dissolve overlay — fades in/out at edges ── */}
        {dissolveOpacity > 0 && (
          <div
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              right: 0,
              bottom: 0,
              background: COLORS.bgGradient,
              opacity: dissolveOpacity,
              pointerEvents: 'none',
            }}
          />
        )}

      </div>

      {/* ── Per-step voiceover (audio tracks, no visual) ── */}
      {cues.map((cue, i) =>
        cue.audioFilename ? (
          <Sequence key={`audio-${i}`} from={cue.startFrame}>
            <Audio src={staticFile(cue.audioFilename)} />
          </Sequence>
        ) : null
      )}

    </AbsoluteFill>
  );
};
