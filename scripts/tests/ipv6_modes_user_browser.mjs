import {readFile,writeFile,mkdir,stat,symlink,rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {networkInterfaces} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
if(!Object.values(networkInterfaces()).flat().some(x=>x?.address==='192.168.0.32'))throw Error('Test server only');
const root=process.cwd(),work='/tmp/cf-modes-e2e-1031',ev=work+'/evidence',base='http://127.0.0.1:15932';
await mkdir(ev,{recursive:true});
const action=process.argv[2]||'inspect',label=process.argv[3]||action;
if(action==='web'){
 const hash=createHash('sha256').update(await readFile(root+'/frontend/package-lock.json')).digest('hex').slice(0,16);
 await rm(root+'/frontend/node_modules',{force:true}).catch(()=>{});await symlink(`/home/sunbroke/cf-test/node_modules/${hash}/node_modules`,root+'/frontend/node_modules');
 const {createServer}=await import(pathToFileURL(root+'/frontend/node_modules/vite/dist/node/index.js'));
 const vite=await createServer({root:root+'/frontend',configFile:root+'/frontend/vite.config.mts',logLevel:'warn',server:{host:'0.0.0.0',port:15932,strictPort:true,proxy:{'/api':{target:'http://127.0.0.1:15931',changeOrigin:true}}}});
 await vite.listen();console.log('WEB READY');await new Promise(()=>{});
}
const {chromium}=await import('/home/sunbroke/rt/private__tmp__configflow-real-env-rebuild__browser-runtime/node_modules/playwright/index.mjs');
const browser=await chromium.launch({headless:true});
const auth=work+'/browser-auth.json';
const context=await browser.newContext({viewport:{width:1440,height:1100},storageState:await stat(auth).then(()=>auth).catch(()=>undefined)});
const page=await context.newPage();page.setDefaultTimeout(20000);
const responses=[],errors=[],pending=[];
function redact(o){if(!o||typeof o!=='object')return o; if(Array.isArray(o))return o.map(redact);return Object.fromEntries(Object.entries(o).map(([k,v])=>[k,/password|token|secret|authorization/i.test(k)?'[REDACTED]':redact(v)]))}
page.on('response',r=>{const u=new URL(r.url());if(!u.pathname.startsWith('/api/')||/auth\//.test(u.pathname))return;pending.push((async()=>{try{const data=await r.json();if(/ipv6|routers|deploy|custom-config|generate/.test(u.pathname)||r.status()>=400)responses.push({at:Date.now(),url:u.pathname+u.search,status:r.status(),method:r.request().method(),data:redact(data)})}catch{}})())});
page.on('pageerror',e=>errors.push(e.message));
async function snap(suffix=''){
 await writeFile(`${ev}/${label}${suffix}.txt`,await page.locator('body').innerText());
 await page.screenshot({path:`${ev}/${label}${suffix}.png`,fullPage:true});
 const pre=await page.locator('pre').allTextContents();await writeFile(`${ev}/${label}${suffix}-commands.json`,JSON.stringify(pre.filter(x=>/^\/(ip|ipv6) /.test(x.trim())),null,2));
}
async function settle(){await page.waitForTimeout(1200);await page.waitForFunction(()=>!document.querySelector('[data-mihomo-ipv6-steps] [data-tone="progress"]'),null,{timeout:90000});await page.waitForTimeout(1500)}
async function settings(){await page.goto(base+'/mihomo');await page.getByRole('button',{name:'Mihomo 设置',exact:true}).click();await page.locator('[data-mihomo-ipv6-settings]').waitFor();await settle()}
async function checks(){await page.getByRole('button',{name:'检查设备 IPv6',exact:true}).click();await page.locator('[data-mihomo-ipv6-dialog]').waitFor();await settle()}
try{
 await page.goto(base+'/mihomo');
 await page.getByPlaceholder('请输入用户名').or(page.getByRole('button',{name:'Mihomo 设置',exact:true})).first().waitFor({timeout:60000});
 if(await page.getByPlaceholder('请输入用户名').isVisible().catch(()=>false)){
  const c=JSON.parse(await readFile('/home/sunbroke/previews/configflow-ipv6-readiness/credentials.json'));
  await page.getByPlaceholder('请输入用户名').fill(c.username);await page.getByPlaceholder('请输入密码').fill(c.password);await page.getByRole('button',{name:'登录',exact:true}).click();await page.waitForURL(u=>!u.pathname.includes('login'));await context.storageState({path:auth});
 }
 if(action==='pick'){
  await page.getByRole('button',{name:'全部展开',exact:true}).click();
  await page.getByText(process.argv[4],{exact:true}).first().click();await page.waitForTimeout(1000);
 }else if(action==='router'){
  await settings();const links=page.locator('[data-publish-upstream-routers] a');
  if(await links.count()){await links.first().click()}else await page.goto(base+'/routers?router=router_d10a2f2c9442&profile=default');
  await page.locator('[data-router-report]').waitFor({timeout:90000});await page.waitForTimeout(1800);await snap('-before');
  if(!await page.getByRole('button',{name:'确认发布',exact:true}).isVisible().catch(()=>false))await page.getByRole('button',{name:'预览发布',exact:true}).click();
  await page.getByRole('button',{name:'确认发布',exact:true}).waitFor();await snap('-preview');
  const button=page.getByRole('button',{name:'确认发布',exact:true});
  if(await button.isEnabled()){await button.click();await page.waitForTimeout(3000);await page.getByRole('button',{name:'关闭',exact:true}).first().waitFor({timeout:90000});await page.waitForTimeout(2000)}
 } else {
  await settings();
  if(action==='mode'){
   const modes={off:'关闭',nodes:'Mihomo 用 IPv6，客户端走 IPv4',clients:'客户端也走 IPv6'};
   await page.getByRole('radio',{name:modes[process.argv[4]],exact:true}).click();
   if(process.argv[5]){await page.getByLabel('IPv6 Fake-IP 网段',{exact:true}).fill(process.argv[5]);await page.getByLabel('IPv6 Fake-IP 网段',{exact:true}).blur()}
   await page.locator('[data-settings-publish-bar]').getByRole('button',{name:'保存草稿',exact:true}).click();
   await page.getByText('基础配置草稿已保存').first().waitFor();await settle();
  }
  if(action==='check'||action==='prepare'||action==='verify'||action==='verify-settings'){
   await checks();await page.getByRole('button',{name:'重新检查',exact:true}).last().click();await settle();
   if(action==='prepare'){
    const btn=page.getByRole('button',{name:/补齐/}).last();await snap('-before');
    if(await btn.isEnabled()){await btn.click();await settle();await page.waitForTimeout(6000)}
   }
   if(action==='verify-settings'){
    await page.getByRole('button',{name:'验收设置',exact:true}).click();await page.getByLabel('检测网址',{exact:true}).fill('http://fixture.lab.invalid:18080/acceptance');
    await page.getByRole('combobox',{name:'独立检测客户端',exact:true}).click();await page.getByRole('option',{name:'测试客户端',exact:true}).click();await page.getByRole('button',{name:'保存验收设置',exact:true}).click();await page.waitForTimeout(1000);
   }
   if(action==='verify'){
    await page.getByRole('button',{name:'验证连接与分流',exact:true}).last().click();await page.waitForTimeout(1500);
    await page.waitForFunction(()=>!document.querySelector('[data-mihomo-ipv6-dialog]')?.textContent?.includes('正在验证连接与分流'),null,{timeout:150000});await page.waitForTimeout(1000);
   }
  }
  if(action==='recheck'){
   const btn=page.locator('[data-ipv6-steps-recheck]');if(await btn.count()){await btn.click();await settle()}
  }
  if(action==='publish'||action==='publish-verify'){
   await page.locator('[data-settings-publish-bar]').getByRole('button',{name:'保存并发布',exact:true}).click();await page.waitForTimeout(2000);
   await page.waitForFunction(()=>{const e=document.querySelector('[data-settings-publish-bar]');return e&&/发布成功|发布失败|已发布|应用并启动|校验失败|发布完成/.test(e.textContent)&&!e.textContent.includes('正在验证连接与分流')},null,{timeout:180000}).catch(()=>{});
   if(action==='publish-verify' && await page.getByRole('button',{name:'验证连接与分流',exact:true}).last().isVisible().catch(()=>false)){await page.getByRole('button',{name:'验证连接与分流',exact:true}).last().click();await page.waitForTimeout(1500);await page.waitForFunction(()=>!document.querySelector('[data-settings-publish-bar]')?.textContent?.includes('正在验证连接与分流'),null,{timeout:150000})}
  }
 }
 await snap();console.log((await page.locator('body').innerText()).slice(-14500));
}catch(e){errors.push(e.stack);await snap('-error').catch(()=>{});console.error(e.stack);process.exitCode=1}
finally{await Promise.allSettled(pending);await writeFile(`${ev}/${label}-api.json`,JSON.stringify({action,label,responses,errors},null,2));await context.storageState({path:auth});await browser.close()}
