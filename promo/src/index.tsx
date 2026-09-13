import React from 'react';
import {registerRoot, Composition} from 'remotion';
import {Preview} from './preview';

const Root = () => <Composition id="Preview29" component={Preview} width={1920} height={1080} fps={30} durationInFrames={870}/>;
registerRoot(Root);
