import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {theme} from './theme';

type Props = {
  points: string[];
};

export const KineticKeywords: React.FC<Props> = ({points}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <div style={{position: 'absolute', left: 76, right: 76, top: 680}}>
      {points.map((point, index) => {
        const localFrame = frame - 48 - index * 24;
        const enter = spring({frame: Math.max(0, localFrame), fps, config: {damping: 16, stiffness: 120}});
        const scan = interpolate((frame + index * 19) % 90, [0, 90], [-20, 120]);
        return (
          <div
            key={point}
            style={{
              position: 'relative',
              overflow: 'hidden',
              marginBottom: 24,
              padding: '28px 34px',
              borderRadius: 26,
              border: `1px solid ${theme.stroke}`,
              background: index % 2 ? 'rgba(124,220,255,.10)' : 'rgba(25,200,137,.10)',
              color: theme.text,
              fontSize: 42,
              fontWeight: 850,
              opacity: enter,
              transform: `translateX(${(1 - enter) * -80}px)`,
            }}
          >
            <span style={{color: index % 2 ? theme.accent2 : theme.accent, marginRight: 18}}>
              0{index + 1}
            </span>
            {point}
            <div
              style={{
                position: 'absolute',
                top: 0,
                bottom: 0,
                left: `${scan}%`,
                width: 90,
                transform: 'skewX(-14deg)',
                background: 'linear-gradient(90deg, transparent, rgba(255,255,255,.22), transparent)',
              }}
            />
          </div>
        );
      })}
    </div>
  );
};
