import React from 'react';
import {AbsoluteFill, Img, interpolate, staticFile} from 'remotion';
import {COLORS, TITLE_FRAMES} from '../constants';
import {FONT_FAMILY} from '../fonts';

interface Props {
  frame: number;
  title: string;
  language: 'he' | 'en';
  titleFrames?: number;
}

export const TitleSlide: React.FC<Props> = ({frame, title, language, titleFrames}) => {
  const duration = titleFrames ?? TITLE_FRAMES;
  const isHeb = language === 'he';
  const fadeIn = interpolate(frame, [0, 20], [0, 1], {extrapolateRight: 'clamp'});
  const fadeOut = interpolate(frame, [duration - 20, duration], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
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
      {/* Brand logo */}
      <Img
        src={staticFile('jeen-logo.png')}
        style={{
          position: 'absolute',
          top: 50,
          left: 80,
          width: 120,
          height: 'auto',
        }}
      />

      {/* Main title */}
      <div
        style={{
          transform: `translateY(${titleY}px)`,
          color: COLORS.text,
          fontSize: 72,
          fontFamily: FONT_FAMILY,
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
          fontFamily: FONT_FAMILY,
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
