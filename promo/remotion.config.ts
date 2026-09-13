import {Config} from '@remotion/cli/config';
import os from 'node:os';
import path from 'node:path';

const data=process.env.CHZ_PROMO_DIR || path.join(os.homedir(),'.local/share/w2f-cli/promo-preview-20260913');
Config.setPublicDir(path.join(data,'assets'));
Config.setCodec('h264');
Config.setPixelFormat('yuv420p');
