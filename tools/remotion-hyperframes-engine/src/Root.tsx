import React from 'react';
import {Composition} from 'remotion';
import {InfoMotion} from './compositions/InfoMotion';

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="InfoMotion"
      component={InfoMotion}
      durationInFrames={360}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={{
        title: 'AI 搜索正在改变普通人获取信息的方式',
        subtitle: '信息入口正在从搜索框变成对话框',
        points: [
          '平台流量重新分配',
          '广告模式受到挑战',
          '内容生态开始重排',
          '普通人需要新的判断标准',
        ],
      }}
    />
  );
};
