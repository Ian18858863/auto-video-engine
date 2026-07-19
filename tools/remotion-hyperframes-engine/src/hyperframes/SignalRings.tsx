import React from 'react';
import {interpolate, useCurrentFrame} from 'remotion';
import {theme} from './theme';

export const SignalRings: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <div style={{position: 'absolute', right: -140, bottom: -100, width: 520, height: 520}}>
      {Array.from({length: 5}).map((_, index) => {
        const progress = ((frame + index * 18) % 120) / 120;
        const scale = interpolate(progress, [0, 1], [0.22, 1.18]);
        const opacity = interpolate(progress, [0, 0.2, 1], [0, 0.5, 0]);
        return (
          <div
            key={index}
            style={{
              position: 'absolute',
              inset: 0,
              borderRadius: '50%',
              border: `3px solid ${index % 2 ? theme.accent2 : theme.accent}`,
              opacity,
              transform: `scale(${scale})`,
              filter: 'blur(.2px)',
            }}
          />
        );
      })}
    </div>
  );
};
