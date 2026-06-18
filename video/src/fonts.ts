import {loadFont as loadUrbanist} from '@remotion/google-fonts/Urbanist';
import {loadFont as loadHeebo} from '@remotion/google-fonts/Heebo';

const urbanist = loadUrbanist();
const heebo = loadHeebo();

export const FONT_FAMILY = `${urbanist.fontFamily}, ${heebo.fontFamily}, sans-serif`;
