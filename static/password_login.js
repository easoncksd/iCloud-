/* Apple passwords live only in the submitted form and encrypted server storage. */
Object.assign(I18N.zh, {
  'login.title':'账号密码登录', 'login.help':'输入 Apple 账号主密码。完成验证后，服务器会加密保存密码和登录态，掉线时尝试自动恢复。',
  'login.username':'Apple 账号邮箱', 'login.password':'Apple 账号主密码',
  'login.saved':'主密码已加密保存；留空使用已保存密码，填写可更新密码。',
  'login.submit':'登录并保存', 'login.busy':'正在连接 Apple…', 'login.cookie':'使用 Cookie 导入',
  'login.code':'Apple 验证码', 'login.code_help':'使用可信设备显示的六位验证码，也可在设备的 Apple 账号安全设置中获取。若选择短信，请先发送短信。验证等待最多 15 分钟。',
  'login.device':'可信设备验证码', 'login.sms':'短信验证码', 'login.send':'发送短信',
  'login.sent':'短信已发送，请选择短信验证码并输入。', 'login.verify':'验证并保存',
  'login.done':'登录态已保存，后续掉线将尝试自动恢复。', 'login.forget':'删除保存的主密码',
  'login.forgot':'已删除保存的主密码，当前会话仍可继续使用。', 'login.pending':'需要 Apple 验证码，点击账号密码登录完成验证',
  'login.saved_status':'服务器已保存登录态', 'login.paused':'自动登录已暂停，请点击账号密码登录检查',
  'login.forget_confirm':'删除服务器保存的 Apple 主密码并关闭自动登录？当前收信设置会保留。'
});
Object.assign(I18N.en, {
  'login.title':'Sign in with password', 'login.help':'Use your primary Apple Account password. After verification, the server encrypts your password and session and attempts recovery when disconnected.',
  'login.username':'Apple Account email', 'login.password':'Apple Account password',
  'login.saved':'Password stored encrypted. Leave blank to reuse it, or enter a replacement.',
  'login.submit':'Sign in and save', 'login.busy':'Connecting to Apple…', 'login.cookie':'Import cookies instead',
  'login.code':'Apple verification code', 'login.code_help':'Enter the six-digit code from a trusted device or its Apple Account security settings. For SMS, send a code first. Verification expires in 15 minutes.',
  'login.device':'Trusted device code', 'login.sms':'SMS code', 'login.send':'Send SMS',
  'login.sent':'SMS sent. Select SMS code and enter it.', 'login.verify':'Verify and save',
  'login.done':'Session saved. Recovery will be attempted after disconnection.', 'login.forget':'Disable automatic sign-in and delete saved password',
  'login.forgot':'Saved password deleted. Your current session remains available.', 'login.pending':'Apple verification required; open password sign-in',
  'login.saved_status':'Server session saved', 'login.paused':'Automatic sign-in paused; open password sign-in',
  'login.forget_confirm':'Delete the saved Apple password and disable automatic sign-in? Mail settings will be kept.'
});
var passwordLoginContext = null;

function showAddAccountModal() { showPasswordLoginModal(); }

function closePasswordLoginModal() {
  var ctx = passwordLoginContext;
  passwordLoginContext = null;
  var modal = E('passwordLoginModal');
  if (modal) modal.remove();
  if (ctx && ctx.loginId) api('/api/password-login/' + ctx.loginId + '/cancel', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
  });
}

function passwordLoginMessage(ctx, text, failed) {
  if (passwordLoginContext !== ctx || !E('passwordLoginMsg')) return;
  E('passwordLoginMsg').textContent = text;
  E('passwordLoginMsg').style.color = failed ? 'var(--red)' : 'var(--green)';
}

function showPasswordLoginModal(accId) {
  closePasswordLoginModal(); closeAddAccModal();
  var acc = accounts.find(function(a) { return a.id === accId; }) || {};
  var ctx = {accId:accId || '', loginId:'', busy:false};
  passwordLoginContext = ctx;
  var h = '<div class="modal-overlay" id="passwordLoginModal" onclick="if(event.target===this)closePasswordLoginModal()"><div class="modal-box" style="max-height:calc(100vh - 48px)">' +
    '<h3>' + t('login.title') + '</h3><p>' + t('login.help') + '</p>' +
    '<div id="passwordLoginForm"><label class="hint">' + t('add.region') + '</label><select id="loginHost">' +
    '<option value="icloud.com"' + (acc.host !== 'icloud.com.cn' ? ' selected' : '') + '>' + t('add.region_intl') + '</option>' +
    '<option value="icloud.com.cn"' + (acc.host === 'icloud.com.cn' ? ' selected' : '') + '>' + t('add.region_cn') + '</option></select>' +
    (accId ? '' : '<input id="loginName" type="text" maxlength="120" placeholder="' + escAttr(t('add.name_ph')) + '">') +
    '<label class="hint">' + t('login.username') + '</label><input id="loginUsername" type="email" autocomplete="username" maxlength="320" value="' + escAttr(acc.real_email || '') + '"' + (accId ? ' readonly' : '') + '>' +
    '<label class="hint">' + t('login.password') + '</label><input id="loginPassword" type="password" autocomplete="current-password" maxlength="1024">' +
    (acc.has_password_login ? '<p class="hint">' + t('login.saved') + '</p>' : '') +
    '<div class="modal-actions" style="flex-wrap:wrap"><button class="btn btn-outline" onclick="passwordLoginCookieFallback()">' + t('login.cookie') + '</button>' +
    '<button class="btn btn-primary" id="loginStartBtn" onclick="submitPasswordLogin()">' + t('login.submit') + '</button></div></div>' +
    '<div id="passwordLoginVerify" style="display:none"></div><div class="modal-msg" id="passwordLoginMsg"></div>' +
    '<div class="modal-actions" style="flex-wrap:wrap"><button class="btn btn-outline" onclick="closePasswordLoginModal()">' + t('action.cancel') + '</button>' +
    (acc.has_password_login ? '<button class="btn btn-outline" id="loginForgetBtn" onclick="forgetPasswordLogin()">' + t('login.forget') + '</button>' : '') +
    '</div></div></div>';
  document.body.insertAdjacentHTML('beforeend', h);
  if (acc.password_login_pending) renderPasswordLoginChallenge(ctx, acc.password_login_pending);
  else E(accId ? 'loginPassword' : 'loginUsername').focus();
}

function passwordLoginCookieFallback() {
  var accId = passwordLoginContext && passwordLoginContext.accId;
  closePasswordLoginModal();
  if (accId) showReimportModal(accId); else showCookieAccountModal();
}

function renderPasswordLoginChallenge(ctx, result) {
  ctx.loginId = result.login_id;
  E('passwordLoginForm').style.display = 'none';
  var phones = result.phones || [];
  E('passwordLoginVerify').innerHTML = '<p>' + t('login.code_help') + '</p>' +
    '<select id="loginCodeMethod"><option value="device">' + t('login.device') + '</option>' +
    (phones.length ? '<option value="sms">' + t('login.sms') + '</option>' : '') + '</select>' +
    (phones.length ? '<select id="loginPhone">' + phones.map(function(p) {
      return '<option value="' + escAttr(p.id) + '">' + esc(p.number) + '</option>';
    }).join('') + '</select><button class="btn btn-outline" id="loginSmsBtn" onclick="sendPasswordLoginSMS()">' + t('login.send') + '</button>' : '') +
    '<label class="hint">' + t('login.code') + '</label><input id="loginCode" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6">' +
    '<div class="modal-actions" style="flex-wrap:wrap"><button class="btn btn-primary" id="loginVerifyBtn" onclick="verifyPasswordLogin()">' + t('login.verify') + '</button></div>';
  E('passwordLoginVerify').style.display = 'block';
  passwordLoginMessage(ctx, t('login.pending'), false);
  E('loginCode').focus();
}

async function submitPasswordLogin() {
  var ctx = passwordLoginContext;
  if (!ctx || ctx.busy) return;
  var payload = {username:E('loginUsername').value.trim(), password:E('loginPassword').value,
    host:E('loginHost').value, name:E('loginName') ? E('loginName').value.trim() || t('add.unnamed') : '', account_id:ctx.accId};
  ctx.busy = true; E('loginStartBtn').disabled = true;
  passwordLoginMessage(ctx, t('login.busy'), false);
  try {
    var promise = api('/api/password-login/start', {method:'POST', timeout:60000,
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    payload.password = ''; E('loginPassword').value = '';
    var result = await promise;
    if (passwordLoginContext !== ctx) {
      if (result.requires_code) api('/api/password-login/' + result.login_id + '/cancel', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      return;
    }
    if (!result.ok) passwordLoginMessage(ctx, result.error || t('error.unknown'), true);
    else if (result.requires_code) renderPasswordLoginChallenge(ctx, result);
    else passwordLoginComplete(ctx);
  } finally {
    ctx.busy = false;
    if (passwordLoginContext === ctx && E('loginStartBtn')) E('loginStartBtn').disabled = false;
  }
}

function passwordLoginComplete(ctx) {
  ctx.loginId = '';
  passwordLoginMessage(ctx, t('login.done'), false);
  refreshAll(true);
  setTimeout(function() { if (passwordLoginContext === ctx) closePasswordLoginModal(); }, 1500);
}

async function sendPasswordLoginSMS() {
  var ctx = passwordLoginContext;
  if (!ctx || ctx.busy) return;
  ctx.busy = true; E('loginSmsBtn').disabled = true;
  try {
    var result = await api('/api/password-login/' + ctx.loginId + '/sms', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone_id:E('loginPhone').value})});
    if (passwordLoginContext !== ctx) return;
    if (result.ok) E('loginCodeMethod').value = 'sms';
    passwordLoginMessage(ctx, result.ok ? t('login.sent') : result.error, !result.ok);
  } finally {
    ctx.busy = false;
    if (passwordLoginContext === ctx && E('loginSmsBtn')) E('loginSmsBtn').disabled = false;
  }
}

async function verifyPasswordLogin() {
  var ctx = passwordLoginContext;
  if (!ctx || ctx.busy) return;
  var code = E('loginCode').value.trim();
  if (!/^[0-9]{6}$/.test(code)) { passwordLoginMessage(ctx, t('login.code'), true); return; }
  ctx.busy = true; E('loginVerifyBtn').disabled = true;
  try {
    var result = await api('/api/password-login/' + ctx.loginId + '/verify', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({code:code, method:E('loginCodeMethod').value})});
    if (passwordLoginContext !== ctx) return;
    E('loginCode').value = '';
    if (result.ok) passwordLoginComplete(ctx);
    else passwordLoginMessage(ctx, result.error || t('error.unknown'), true);
  } finally {
    ctx.busy = false;
    if (passwordLoginContext === ctx && E('loginVerifyBtn')) E('loginVerifyBtn').disabled = false;
  }
}

async function forgetPasswordLogin() {
  var ctx = passwordLoginContext;
  if (!ctx || ctx.busy || !confirm(t('login.forget_confirm'))) return;
  ctx.busy = true;
  try {
    var result = await api('/api/password-login/forget', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({account_id:ctx.accId})});
    passwordLoginMessage(ctx, result.ok ? t('login.forgot') : result.error, !result.ok);
    if (result.ok) { ctx.loginId = ''; refreshAll(true); closePasswordLoginModal(); }
  } finally { ctx.busy = false; }
}
