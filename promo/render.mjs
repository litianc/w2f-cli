import {bundle} from '@remotion/bundler';
import {selectComposition, renderMedia, renderStill} from '@remotion/renderer';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {mkdir} from 'node:fs/promises';

const root=path.dirname(fileURLToPath(import.meta.url));
const data=process.env.W2F_PROMO_DIR || process.env.CHZ_PROMO_DIR || path.join(os.homedir(),'.local/share/w2f-cli/promo-preview-20260913');
const publicDir=path.join(data,'assets');
await mkdir(path.join(data,'frames'),{recursive:true});
const serveUrl=await bundle({entryPoint:path.join(root,'src/index.tsx'),publicDir});
const composition=await selectComposition({serveUrl,id:'Preview29'});
const stills=process.argv.includes('--stills');
if(stills){
 for(const frame of [45,120,240,350,390,510,660,810,869]){
  await renderStill({serveUrl,composition,frame,output:path.join(data,'frames',`${frame}.png`)});
  console.log(`Frame ${frame} ready`);
 }
}else{
 let last=-1;
 await renderMedia({serveUrl,composition,codec:'h264',crf:17,pixelFormat:'yuv420p',audioCodec:'aac',audioBitrate:'256k',concurrency:3,outputLocation:path.join(data,'W2F-CLI-29秒预览.mp4'),onProgress:({progress})=>{const p=Math.floor(progress*10);if(p>last){last=p;console.log(`${p*10}%`);}}});
 console.log(path.join(data,'W2F-CLI-29秒预览.mp4'));
}
