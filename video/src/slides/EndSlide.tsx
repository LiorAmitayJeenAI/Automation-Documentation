import React from 'react';
import {AbsoluteFill, interpolate} from 'remotion';
import {COLORS} from '../constants';

interface Props {
  frame: number;
  language: 'he' | 'en';
}

export const EndSlide: React.FC<Props> = ({frame, language}) => {
  const isHeb = language === 'he';
  const opacity = interpolate(frame, [0, 18], [0, 1], {extrapolateRight: 'clamp'});

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
      <div
        style={{
          color: COLORS.accent,
          fontSize: 38,
          fontFamily: 'Arial, sans-serif',
          fontWeight: 700,
          letterSpacing: 3,
          marginBottom: 24,
        }}
      >
        JEEN
      </div>

      <div
        style={{
          color: COLORS.text,
          fontSize: 60,
          fontFamily: 'Arial, sans-serif',
          fontWeight: 700,
          direction: isHeb ? 'rtl' : 'ltr',
        }}
      >
        {isHeb ? 'תודה שצפיתם' : 'Thank You'}
      </div>

      <div
        style={{
          color: COLORS.textMuted,
          fontSize: 28,
          fontFamily: 'Arial, sans-serif',
          marginTop: 18,
          direction: isHeb ? 'rtl' : 'ltr',
        }}
      >
        {isHeb ? 'jeenai.app' : 'jeenai.app'}
      </div>

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
