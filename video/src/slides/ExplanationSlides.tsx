/**
 * Renders explanation slides for steps that Playwright could not navigate to.
 * Each slide shows the Hebrew narration as a prominent text card so customers
 * still receive the full explanation even when the live product screen is
 * unavailable.
 *
 * Frame 0 here = start of the explanation sequence (after the recording).
 */
import React from 'react';
import {AbsoluteFill, useCurrentFrame, interpolate} from 'remotion';
import {COLORS, EXPLANATION_FRAMES} from '../constants';
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
          background: 'linear-gradient(to bottom, rgba(11,11,26,0.9) 0%, transparent 100%)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0 64px',
        }}
      >
        <div
          style={{
            color: COLORS.accent,
            fontSize: 24,
            fontFamily: 'Arial, sans-serif',
            fontWeight: 700,
            letterSpacing: 3,
          }}
        >
          JEEN
        </div>
        <div
          style={{
            color: 'rgba(255,255,255,0.7)',
            fontSize: 18,
            fontFamily: 'Arial, sans-serif',
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
            fontFamily: 'Arial, sans-serif',
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
              fontFamily: 'Arial, sans-serif',
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
          fontFamily: 'Arial, sans-serif',
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
    </AbsoluteFill>
  );
};
