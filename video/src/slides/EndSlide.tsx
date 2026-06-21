import React from 'react';
import {AbsoluteFill, Img, interpolate, staticFile} from 'remotion';
import {COLORS} from '../constants';
import {FONT_FAMILY} from '../fonts';

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
      <Img
        src={staticFile('jeen-logo.png')}
        style={{width: 160, height: 'auto', marginBottom: 24}}
      />

      <div
        style={{
          color: COLORS.text,
          fontSize: 60,
          fontFamily: FONT_FAMILY,
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
          fontFamily: FONT_FAMILY,
          marginTop: 18,
          direction: isHeb ? 'rtl' : 'ltr',
        }}
      >
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
