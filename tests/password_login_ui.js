// Run with NODE_PATH pointing at an installed Playwright package.
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = fs.readFileSync(path.join(root, 'static/password_login.js'), 'utf8');
const output = process.argv[3];

async function check(browser, width) {
  const height = width < 500 ? 667 : 900;
  const page = await browser.newPage({viewport:{width,height}});
  const errors = [];
  const calls = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.route('http://ui.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:html});
    if (url.pathname === '/static/password_login.js') return route.fulfill({contentType:'application/javascript',body:script});
    if (url.pathname === '/api/accounts') return route.fulfill({json:{accounts:[{
      id:'a',name:'测试账号',real_email:'demo@example.com',host:'icloud.com',status:'active',
      has_password_login:true,has_app_password:true,mail_status:'ok'
    }]}});
    if (url.pathname === '/api/state') return route.fulfill({json:{running:false,creating:false}});
    if (url.pathname === '/api/emails') return route.fulfill({json:{emails:[]}});
    if (url.pathname === '/api/creation-guard') return route.fulfill({json:{ok:true,protection:{settings:{concurrency:3,daily_limit:50},accounts:{},events:[]}}});
    if (url.pathname === '/api/log-stream') return route.fulfill({contentType:'text/event-stream',body:''});
    if (url.pathname.startsWith('/api/password-login')) {
      calls.push({url:url.pathname,body:route.request().postDataJSON()});
      if (url.pathname.endsWith('/start')) return route.fulfill({json:{ok:true,requires_code:true,
        login_id:'01234567890123456789012345678901',phones:[{id:'1',number:'•••• 78'}]}});
      if (url.pathname.endsWith('/verify')) {
        const good = route.request().postDataJSON().code === '123456';
        return route.fulfill({json:good ? {ok:true,id:'a',requires_code:false} : {ok:false,error:'验证码无效 <img src=x onerror=alert(1)>'}});
      }
    }
    return route.fulfill({json:{ok:true}});
  });
  await page.goto('http://ui.test/',{waitUntil:'domcontentloaded'});
  if (errors.length) throw Error(JSON.stringify(errors));
  await page.click('[data-tab="accounts"]');
  await page.locator('.acc-actions').getByRole('button',{name:'账号密码登录',exact:true}).click();
  if (!(await page.locator('#loginUsername').getAttribute('readonly') === '')) throw Error('Existing account identity should be fixed');
  await page.locator('#loginStartBtn').click();
  await page.locator('#loginCode').waitFor();
  await page.locator('#loginSmsBtn').click();
  await page.waitForFunction(() => document.getElementById('loginCodeMethod').value === 'sms');
  await page.fill('#loginCode','000000');
  await page.locator('#loginVerifyBtn').click();
  await page.waitForFunction(() => document.getElementById('passwordLoginMsg').textContent.includes('验证码无效'));
  if (await page.locator('#passwordLoginMsg img').count()) throw Error('Unescaped login error');
  if (await page.inputValue('#loginCode') !== '') throw Error('Verification code not cleared');
  await page.fill('#loginCode','123456');
  await page.locator('#loginVerifyBtn').click();
  await page.waitForFunction(() => document.getElementById('passwordLoginMsg').textContent.includes('登录态已保存'));
  await page.waitForTimeout(1600);
  await page.evaluate(() => showAddAccountModal());
  await page.fill('#loginUsername','new@example.com');
  await page.fill('#loginPassword','dummy-test-password');
  await page.locator('#loginStartBtn').click();
  await page.locator('#loginCode').waitFor();
  if (await page.inputValue('#loginPassword') !== '') throw Error('Submitted password not cleared');
  await page.evaluate(() => closePasswordLoginModal());
  await page.evaluate(() => showAddAccountModal());
  await page.getByRole('button',{name:'使用 Cookie 导入',exact:true}).click();
  await page.locator('#cookieInput').waitFor();
  await page.evaluate(() => closeAddAccModal());
  await page.locator('.acc-actions').getByRole('button',{name:'账号密码登录',exact:true}).click();
  await page.waitForTimeout(350);
  const metrics = await page.evaluate(() => {
    const rect = document.getElementById('passwordLoginModal').querySelector('.modal-box').getBoundingClientRect();
    return {width:innerWidth,scrollWidth:document.documentElement.scrollWidth,left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom};
  });
  if (metrics.left < 0 || metrics.right > width || metrics.scrollWidth > width + 1 || metrics.top < 0 || metrics.bottom > height) throw Error('Login modal overflows: '+JSON.stringify(metrics));
  await page.screenshot({path:path.join(output,`password-login-${width}.png`),fullPage:true});
  if (errors.length) throw Error(JSON.stringify(errors));
  if (!calls.some(c => c.url.endsWith('/start') && c.body.password === '') ||
      !calls.some(c => c.url.endsWith('/verify') && c.body.method === 'sms')) throw Error('Missing saved-password or SMS flow');
  await page.close();
  return {width,ok:true};
}

(async () => {
  const browser = await chromium.launch({headless:true,executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  try { console.log(JSON.stringify([await check(browser,390),await check(browser,1440)])); }
  finally { await browser.close(); }
})().catch(error => { console.error(error.stack); process.exitCode=1; });
