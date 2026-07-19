import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {theme} from './theme';

type Props = {
  title: string;
  subtitle: string;
};

export const GlassInfoCard: React.FC<Props> = ({title, subtitle}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 18, stiffness: 90}});
  const glow = interpolate(frame % 120, [0, 60, 120], [0.35, 0.7, 0.35]);

  return (
    <div
      style={{
        position: 'absolute',
        left: 70,
        right: 70,
        top: 180,
        padding: '58px 54px 52px',
        borderRadius: 38,
        border: `1px solid ${theme.stroke}`,
        background: `linear-gradient(135deg, ${theme.panelStrong}, rgba(255,255,255,.055))`,
        boxShadow: `0 30px 90px rgba(0,0,0,.34), 0 0 ${80 * glow}px rgba(25,200,137,.18)`,
        transform: `translateY(${(1 - enter) * 70}px) scale(${0.96 + enter * 0.04})`,
        opacity: enter,
        backdropFilter: 'blur(18px)',
      }}
    >
      <div
        style={{
          display: 'inline-flex',
          padding: '10px 18px',
          borderRadius: 999,
          color: theme.accent,
          background: 'rgba(25,200,137,.12)',
          border: '1px solid rgba(25,200,137,.24)',
          fontSize: 28,
          fontWeight: 800,
          marginBottom: 30,
        }}
      >
        趋势信号
      </div>
      <div style={{fontSize: 74, lineHeight: 1.08, fontWeight: 900, color: theme.text}}>
        {title}
      </div>
      <div style={{fontSize: 36, lineHeight: 1.35, color: theme.muted, marginTop: 28}}>
        {subtitle}
      </div>
    </div>
  );
};
