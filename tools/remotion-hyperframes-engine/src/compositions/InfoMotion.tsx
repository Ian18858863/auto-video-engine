import React from 'react';
import {AbsoluteFill} from 'remotion';
import {
  DataStreamBackground,
  GlassInfoCard,
  KineticKeywords,
  SignalRings,
} from '../hyperframes';
import {theme} from '../hyperframes/theme';

type Props = {
  title: string;
  subtitle: string;
  points: string[];
};

export const InfoMotion: React.FC<Props> = ({title, subtitle, points}) => {
  return (
    <AbsoluteFill style={{fontFamily: 'Microsoft YaHei, Noto Sans SC, Arial, sans-serif'}}>
      <DataStreamBackground />
      <GlassInfoCard title={title} subtitle={subtitle} />
      <KineticKeywords points={points} />
      <SignalRings />
      <div
        style={{
          position: 'absolute',
          left: 76,
          right: 76,
          bottom: 120,
          color: theme.muted,
          fontSize: 30,
          lineHeight: 1.4,
          borderTop: `1px solid ${theme.stroke}`,
          paddingTop: 24,
        }}
      >
        用大白话拆解新趋势：不是复述热点，而是看懂背后的入口、利益和普通人判断标准。
      </div>
    </AbsoluteFill>
  );
};
