export interface SubtitleCue {
  /** Frames from recording start (not from composition start). */
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
}

export interface VideoProps {
  title: string;
  language: 'he' | 'en';
  /** Path relative to Remotion public/, e.g. "recordings/session_id.webm" */
  recordedVideoFilename: string;
  /** Duration of the recording in frames (total_seconds × FPS). */
  recordedVideoFrames: number;
  /** Total composition frames = TITLE_FRAMES + recordedVideoFrames + explanationFrames + END_FRAMES. */
  totalFrames: number;
  cues: SubtitleCue[];
  /** Steps that Playwright could not navigate to, shown as explanation slides. */
  explanationCues: ExplanationCue[];
  /** Total frames allocated for all explanation slides. */
  explanationFrames: number;
}
