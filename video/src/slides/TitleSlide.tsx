import React from 'react';
import {AbsoluteFill, interpolate} from 'remotion';
import {COLORS, TITLE_FRAMES} from '../constants';

interface Props {
  frame: number;
  title: string;
  language: 'he' | 'en';
}

export const TitleSlide: React.FC<Props> = ({frame, title, language}) => {
  const isHeb = language === 'he';
  const fadeIn = interpolate(frame, [0, 20], [0, 1], {extrapolateRight: 'clamp'});
  const fadeOut = interpolate(frame, [TITLE_FRAMES - 20, TITLE_FRAMES], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const opacity = Math.min(fadeIn, fadeOut);

  const titleY = interpolate(frame, [0, 25], [30, 0], {extrapolateRight: 'clamp'});

  return (
    <AbsoluteFill
      style={{
        background: COLORS.bgGradient,
        alignItems: 'center',
        justifyContent: 'center',
        flexDirection: 'column',
        opacity,
      }}
    >
      {/* Brand */}
      <div
        style={{
          position: 'absolute',
          top: 60,
          left: 80,
          color: COLORS.accent,
          fontSize: 30,
          fontFamily: 'Arial, sans-serif',
          fontWeight: 700,
          letterSpacing: 3,
        }}
      >
        JEEN
      </div>

      {/* Main title */}
      <div
        style={{
          transform: `translateY(${titleY}px)`,
          color: COLORS.text,
          fontSize: 72,
          fontFamily: 'Arial, sans-serif',
          fontWeight: 700,
          textAlign: 'center',
          direction: isHeb ? 'rtl' : 'ltr',
          maxWidth: 1400,
          lineHeight: 1.35,
          padding: '0 80px',
        }}
      >
        {title}
      </div>

      {/* Subtitle */}
      <div
        style={{
          color: COLORS.textMuted,
          fontSize: 34,
          fontFamily: 'Arial, sans-serif',
          marginTop: 28,
          direction: isHeb ? 'rtl' : 'ltr',
        }}
      >
        {isHeb ? 'מדריך למשתמש' : 'User Guide'}
      </div>

      {/* Accent bar */}
      <div
        style={{
          position: 'absolute',
          bottom: 0,
          left: 0,
          right: 0,
          height: 6,
          background: `linear-gradient(90deg, ${COLORS.accent}, ${COLORS.accentLight}, ${COLORS.accent})`,
        }}
      />
    </AbsoluteFill>
  );
};
