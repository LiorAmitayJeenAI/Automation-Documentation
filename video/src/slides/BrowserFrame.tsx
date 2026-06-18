/**
 * Browser window mockup chrome — traffic-light dots, centered URL pill, dark slate bar.
 * Positioning, border-radius, overflow, and box-shadow are handled by the parent so
 * this component focuses purely on the chrome UI.
 */
import React from 'react';
import {FONT_FAMILY} from '../fonts';

interface Props {
  url?: string;
  children: React.ReactNode;
}

const CHROME_H = 44;
const DOTS = ['#FF5F57', '#FEBC2E', '#28C840'];
// Width of the dot cluster (3 dots × 14px + 2 gaps × 8px) used to balance the pill centering
const DOT_CLUSTER_W = 3 * 14 + 2 * 8; // 58px

export const BrowserFrame: React.FC<Props> = ({url = 'jeenai.app', children}) => {
  return (
    <div style={{width: '100%', height: '100%', display: 'flex', flexDirection: 'column'}}>

      {/* Chrome bar */}
      <div
        style={{
          height: CHROME_H,
          background: '#3D2B3A',
          borderBottom: '1px solid rgba(255,255,255,0.06)',
          display: 'flex',
          alignItems: 'center',
          padding: '0 18px',
          flexShrink: 0,
        }}
      >
        {/* Traffic-light dots */}
        <div style={{display: 'flex', gap: 8, flexShrink: 0}}>
          {DOTS.map((color, i) => (
            <div
              key={i}
              style={{width: 14, height: 14, borderRadius: '50%', background: color}}
            />
          ))}
        </div>

        {/* URL pill — truly centered by using equal-width flex spacers */}
        <div style={{flex: 1, display: 'flex', justifyContent: 'center'}}>
          <div
            style={{
              background: 'rgba(255,255,255,0.07)',
              border: '1px solid rgba(255,255,255,0.1)',
              borderRadius: 999,
              padding: '5px 22px',
              color: 'rgba(255,255,255,0.5)',
              fontSize: 15,
              fontFamily: FONT_FAMILY,
              letterSpacing: 0.2,
              maxWidth: 520,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {url}
          </div>
        </div>

        {/* Balancing spacer matching dot cluster width */}
        <div style={{width: DOT_CLUSTER_W, flexShrink: 0}} />
      </div>

      {/* Content area */}
      <div style={{flex: 1, overflow: 'hidden', position: 'relative', background: '#000'}}>
        {children}
      </div>
    </div>
  );
};
