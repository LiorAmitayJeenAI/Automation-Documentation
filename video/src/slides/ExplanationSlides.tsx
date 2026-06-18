/**
 * Renders explanation slides for steps that Playwright could not navigate to.
 * Each slide shows the Hebrew narration as a prominent text card so customers
 * still receive the full explanation even when the live product screen is
 * unavailable.
 *
 * Frame 0 here = start of the explanation sequence (after the recording).
 */
import React from 'react';
import {AbsoluteFill, Audio, Img, Sequence, staticFile, useCurrentFrame, interpolate} from 'remotion';
import {COLORS, EXPLANATION_FRAMES} from '../constants';
import {FONT_FAMILY} from '../fonts';
import {ExplanationCue} from '../types';

interface Props {
  cues: ExplanationCue[];
  language: 'he' | 'en';
  title: string;
}

export const ExplanationSlides: React.FC<Props> = ({cues, language, title}) => {
  const frame = useCurrentFrame();
  const isHeb = language === 'he';

  const cueIndex = Math.min(Math.floor(frame / EXPLANATION_FRAMES), cues.length - 1);
  const cue = cues[cueIndex];
  const localFrame = frame - cueIndex * EXPLANATION_FRAMES;

  const fadeIn = interpolate(localFrame, [0, 20], [0, 1], {extrapolateRight: 'clamp'});
  const fadeOut = interpolate(
    localFrame,
    [EXPLANATION_FRAMES - 20, EXPLANATION_FRAMES],
    [1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const opacity = Math.min(fadeIn, fadeOut);

  const textY = interpolate(localFrame, [0, 25], [24, 0], {extrapolateRight: 'clamp'});

  if (!cue) return null;

  return (
    <AbsoluteFill style={{ background: COLORS.bgGradient }}>
    <AbsoluteFill
      style={{
        opacity,
      }}
    >
      {/* Top bar: branding + title */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 90,
          background: 'linear-gradient(to bottom, rgba(99,64,94,0.5) 0%, transparent 100%)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0 64px',
        }}
      >
        <Img
          src={staticFile('jeen-logo.png')}
          style={{width: 100, height: 'auto'}}
        />
        <div
          style={{
            color: COLORS.textMuted,
            fontSize: 18,
            fontFamily: FONT_FAMILY,
            fontWeight: 500,
            direction: isHeb ? 'rtl' : 'ltr',
            maxWidth: 800,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {title}
        </div>
      </div>

      {/* Centre content */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          bottom: 0,
          left: 0,
          right: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '120px 120px 100px',
        }}
      >
        {/* Info badge */}
        <div
          style={{
            width: 64,
            height: 64,
            borderRadius: '50%',
            background: `linear-gradient(135deg, ${COLORS.accent}, ${COLORS.accentLight})`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: 40,
            boxShadow: `0 0 40px ${COLORS.glow}`,
            fontSize: 32,
            color: '#fff',
            fontFamily: FONT_FAMILY,
            fontWeight: 700,
          }}
        >
          i
        </div>

        {/* Narration text */}
        <div
          style={{
            transform: `translateY(${textY}px)`,
            maxWidth: 1100,
            textAlign: 'center',
          }}
        >
          <p
            style={{
              color: COLORS.text,
              fontSize: 36,
              fontFamily: FONT_FAMILY,
              textAlign: isHeb ? 'right' : 'center',
              direction: isHeb ? 'rtl' : 'ltr',
              lineHeight: 1.65,
              margin: 0,
            }}
          >
            {cue.text}
          </p>
        </div>
      </div>

      {/* Slide counter — bottom-left for Hebrew (RTL), bottom-right for LTR */}
      <div
        style={{
          position: 'absolute',
          bottom: 32,
          ...(isHeb ? { left: 64 } : { right: 64 }),
          color: COLORS.textMuted,
          fontSize: 20,
          fontFamily: FONT_FAMILY,
          fontWeight: 600,
        }}
      >
        {cueIndex + 1}&nbsp;/&nbsp;{cues.length}
      </div>

      {/* Bottom accent line */}
      <div
        style={{
          position: 'absolute',
          bottom: 0,
          left: 0,
          right: 0,
          height: 4,
          background: `linear-gradient(90deg, ${COLORS.accent}, ${COLORS.accentLight})`,
        }}
      />
    </AbsoluteFill>

      {/* Per-slide voiceover audio */}
      {cues.map((c, i) =>
        c.audioFilename ? (
          <Sequence key={`expl-audio-${i}`} from={i * EXPLANATION_FRAMES}>
            <Audio src={staticFile(c.audioFilename)} />
          </Sequence>
        ) : null
      )}
    </AbsoluteFill>
  );
};
