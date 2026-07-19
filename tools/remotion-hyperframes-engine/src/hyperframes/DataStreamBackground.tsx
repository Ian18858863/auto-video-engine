import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {theme} from './theme';

export const DataStreamBackground: React.FC = () => {
  const frame = useCurrentFrame();
  const {width, height} = useVideoConfig();
  const drift = interpolate(frame % 120, [0, 120], [0, 160]);

  return (
    <AbsoluteFill
      style={{
        background:
          `radial-gradient(circle at 25% 15%, rgba(25,200,137,.22), transparent 30%),` +
          `radial-gradient(circle at 80% 35%, rgba(124,220,255,.18), transparent 28%),` +
          `linear-gradient(160deg, ${theme.bg}, #0d1d24 55%, #10130f)`,
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          position: 'absolute',
          inset: 0,
          backgroundImage:
            'linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px)',
          backgroundSize: '72px 72px',
          transform: `translateY(${drift - 160}px)`,
        }}
      />
      {Array.from({length: 18}).map((_, index) => {
        const x = (index * 137) % width;
        const delay = index * 11;
        const y = ((frame * (2.2 + (index % 4) * 0.4) + delay * 20) % (height + 420)) - 240;
        return (
          <div
            key={index}
            style={{
              position: 'absolute',
              left: x,
              top: y,
              width: 2,
              height: 180 + (index % 5) * 40,
              borderRadius: 999,
              background: `linear-gradient(180deg, transparent, ${index % 2 ? theme.accent2 : theme.accent}, transparent)`,
              opacity: 0.22,
              filter: 'blur(.2px)',
            }}
          />
        );
      })}
    </AbsoluteFill>
  );
};
