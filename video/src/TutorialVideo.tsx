import React from 'react';
import {AbsoluteFill, Audio, Sequence, staticFile, useCurrentFrame} from 'remotion';
import {TITLE_FRAMES, END_FRAMES} from './constants';
import {VideoProps} from './types';
import {TitleSlide} from './slides/TitleSlide';
import {VideoSection} from './slides/VideoSection';
import {ExplanationSlides} from './slides/ExplanationSlides';
import {EndSlide} from './slides/EndSlide';

// Place a royalty-free MP3 at video/public/music/bg.mp3 then set this to true.
const HAS_BG_MUSIC = false;

export const TutorialVideo: React.FC<VideoProps> = ({
  title,
  language,
  recordedVideoFilename,
  recordedVideoFrames,
  titleFrames,
  segments,
  cues,
  explanationCues,
  explanationFrames,
  titleAudioFilename,
  endAudioFilename,
}) => {
  const frame = useCurrentFrame();
  const actualTitleFrames = titleFrames ?? TITLE_FRAMES;
  const videoStart = actualTitleFrames;
  const explanationStart = actualTitleFrames + recordedVideoFrames;
  const endStart = explanationStart + (explanationFrames ?? 0);

  return (
    <AbsoluteFill>
      {/* Background music — low volume, loops across the whole composition */}
      {HAS_BG_MUSIC && (
        <Audio src={staticFile('music/bg.mp3')} volume={0.07} loop />
      )}

      {/* Title card voiceover */}
      {titleAudioFilename && (
        <Sequence from={0} durationInFrames={actualTitleFrames}>
          <Audio src={staticFile(titleAudioFilename)} />
        </Sequence>
      )}

      {/* Title card */}
      {frame < videoStart && (
        <TitleSlide frame={frame} title={title} language={language} titleFrames={actualTitleFrames} />
      )}

      {/* Real product recording (jump-cut segments) + subtitle overlays */}
      {frame >= videoStart && frame < explanationStart && (
        <Sequence from={videoStart} durationInFrames={recordedVideoFrames}>
          <VideoSection
            recordedVideoFilename={recordedVideoFilename}
            segments={segments}
            cues={cues}
            language={language}
            title={title}
            durationInFrames={recordedVideoFrames}
          />
        </Sequence>
      )}

      {/* Explanation slides for steps Playwright could not navigate to */}
      {explanationCues && explanationCues.length > 0 &&
        frame >= explanationStart && frame < endStart && (
        <Sequence from={explanationStart} durationInFrames={explanationFrames}>
          <ExplanationSlides
            cues={explanationCues}
            language={language}
            title={title}
          />
        </Sequence>
      )}

      {/* End card voiceover */}
      {endAudioFilename && (
        <Sequence from={endStart}>
          <Audio src={staticFile(endAudioFilename)} />
        </Sequence>
      )}

      {/* End card */}
      {frame >= endStart && (
        <EndSlide frame={frame - endStart} language={language} />
      )}
    </AbsoluteFill>
  );
};
