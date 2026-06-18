export interface SubtitleCue {
  /** Frames from the start of the jump-cut output timeline. */
  startFrame: number;
  text: string;          // full Hebrew narration (spoken as VO)
  action: string;        // short English description for debugging
  /** Short Hebrew on-screen label (5-8 words). Shown in the subtitle pill instead of full narration. */
  caption?: string;
  /** Path relative to Remotion public/, e.g. "audio/session_id/step_0.mp3". Optional. */
  audioFilename?: string;
}

export interface ExplanationCue {
  text: string;   // Hebrew narration for a step that could not be recorded
  action: string; // short English description
  /** Path relative to Remotion public/, e.g. "audio/session_id/step_950.mp3". Optional. */
  audioFilename?: string;
}

export interface VideoSegment {
  /** Frame in the source WebM where this segment starts. */
  sourceStartFrame: number;
  /** How many frames to show from this segment. */
  durationFrames: number;
  /** Where this segment starts in the output timeline. */
  outputStartFrame: number;
}

export interface VideoProps {
  title: string;
  language: 'he' | 'en';
  /** Path relative to Remotion public/, e.g. "recordings/session_id.webm" */
  recordedVideoFilename: string;
  /** Duration of the jump-cut output in frames (sum of segment durations). */
  recordedVideoFrames: number;
  /** Duration of the title slide in frames (adapts to audio length). */
  titleFrames?: number;
  /** Total composition frames = titleFrames + recordedVideoFrames + explanationFrames + END_FRAMES. */
  totalFrames: number;
  /** Jump-cut segments: only the visible-content portions of the recording. */
  segments: VideoSegment[];
  cues: SubtitleCue[];
  /** Steps that Playwright could not navigate to, shown as explanation slides. */
  explanationCues: ExplanationCue[];
  /** Total frames allocated for all explanation slides. */
  explanationFrames: number;
  /** Audio for the title card. Path relative to Remotion public/. */
  titleAudioFilename?: string;
  /** Audio for the end card. Path relative to Remotion public/. */
  endAudioFilename?: string;
}
