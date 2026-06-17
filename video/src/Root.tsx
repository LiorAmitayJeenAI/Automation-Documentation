import React from 'react';
import {Composition} from 'remotion';
import {TutorialVideo} from './TutorialVideo';
import {VideoProps} from './types';
import {FPS, TITLE_FRAMES, END_FRAMES} from './constants';

const DEFAULT_PROPS: VideoProps = {
  title: 'Tutorial',
  language: 'he',
  recordedVideoFilename: '',
  recordedVideoFrames: FPS * 30,  // 30 s placeholder
  totalFrames: TITLE_FRAMES + FPS * 30 + END_FRAMES,
  cues: [],
  explanationCues: [],
  explanationFrames: 0,
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="TutorialVideo"
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      component={TutorialVideo as any}
      fps={FPS}
      width={1920}
      height={1080}
      durationInFrames={DEFAULT_PROPS.totalFrames}
      defaultProps={DEFAULT_PROPS as unknown as Record<string, unknown>}
      calculateMetadata={({props}) => ({
        durationInFrames: (props as unknown as VideoProps).totalFrames,
      })}
    />
  );
};
