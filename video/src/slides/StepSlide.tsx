import React from 'react';
import {AbsoluteFill, Img, interpolate, staticFile} from 'remotion';
import {COLORS, STEP_FRAMES} from '../constants';
import {VideoStep} from '../types';

interface Props {
  frame: number;
  step: VideoStep;
  stepNumber: number;
  totalSteps: number;
  language: 'he' | 'en';
}

export const StepSlide: React.FC<Props> = ({
  frame,
  step,
  stepNumber,
  totalSteps,
  language,
}) => {
  const isHeb = language === 'he';

  // Screenshot fades in quickly
  const imgOpacity = interpolate(frame, [0, 18], [0, 1], {extrapolateRight: 'clamp'});

  // Narration slides up and fades in slightly after screenshot
  const narrationY = interpolate(frame, [12, 35], [30, 0], {extrapolateRight: 'clamp'});
  const narrationOpacity = interpolate(frame, [12, 35], [0, 1], {extrapolateRight: 'clamp'});

  // Fade out near the end of this slide
  const globalOpacity = interpolate(
    frame,
    [STEP_FRAMES - 18, STEP_FRAMES],
    [1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );

  const screenshotPath = staticFile(
    `screenshots/${step.sessionId}/${step.screenshotFilename}`,
  );

  return (
    <AbsoluteFill
      style={{background: COLORS.bg, opacity: globalOpacity}}
    >
      {/* Brand */}
      <div
        style={{
          position: 'absolute',
          top: 38,
          left: 72,
          color: COLORS.accent,
          fontSize: 26,
          fontFamily: 'Arial, sans-serif',
          fontWeight: 700,
          letterSpacing: 3,
          zIndex: 10,
        }}
      >
        JEEN
      </div>

      {/* Step counter */}
      <div
        style={{
          position: 'absolute',
          top: 38,
          right: 72,
          color: COLORS.textMuted,
          fontSize: 26,
          fontFamily: 'Arial, sans-serif',
          fontWeight: 600,
          zIndex: 10,
        }}
      >
        {stepNumber}&nbsp;/&nbsp;{totalSteps}
      </div>

      {/* Screenshot container */}
      <div
        style={{
          position: 'absolute',
          top: 96,
          left: 72,
          right: 72,
          bottom: 164,
          opacity: imgOpacity,
          borderRadius: 10,
          overflow: 'hidden',
          border: `1px solid ${COLORS.border}`,
          boxShadow: `0 0 60px ${COLORS.glow}`,
        }}
      >
        <Img
          src={screenshotPath}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'contain',
            background: '#ffffff',
            display: 'block',
          }}
        />
      </div>

      {/* Narration bar */}
      <div
        style={{
          position: 'absolute',
          bottom: 0,
          left: 0,
          right: 0,
          minHeight: 148,
          padding: '22px 80px',
          background: COLORS.narrationBg,
          borderTop: `1px solid ${COLORS.border}`,
          transform: `translateY(${narrationY}px)`,
          opacity: narrationOpacity,
          display: 'flex',
          alignItems: 'center',
        }}
      >
        <p
          style={{
            color: COLORS.text,
            fontSize: 30,
            fontFamily: 'Arial, sans-serif',
            textAlign: isHeb ? 'right' : 'left',
            direction: isHeb ? 'rtl' : 'ltr',
            margin: 0,
            lineHeight: 1.55,
            width: '100%',
          }}
        >
          {step.narration}
        </p>
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
  );
};
