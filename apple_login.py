"""Bounded Apple SRP login and user-supplied MFA; no plaintext session files.

Protocol reference: https://github.com/timlaing/pyicloud (SRP and HSA2).
Security keys and additional Apple account challenges require the Apple website.
"""
import base64
import copy
import hashlib
import json
import time
import uuid

import requests
import srp

from network_proxy import configure_session, load_config

srp.rfc5054_enable()
srp.no_username_in_x()
WIDGET_KEY = 'd39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d'
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.1 Safari/605.1.15')
TOKEN_HEADERS = {'X-Apple-ID-Account-Country': 'account_country',
                 'X-Apple-ID-Session-Id': 'session_id',
                 'X-Apple-Session-Token': 'session_token',
                 'X-Apple-TwoSV-Trust-Token': 'trust_token',
                 'scnt': 'scnt', 'X-Apple-Auth-Attributes': 'auth_attributes'}


class AppleLoginError(ValueError):
    def __init__(self, message, kind='auth'):
        super().__init__(message)
        self.kind = kind


class _Password:
    def __init__(self, password):
        self.digest = hashlib.sha256(password.encode()).digest()

    def prepare(self, data):
        self.salt = base64.b64decode(data['salt'], validate=True)
        self.iterations = data['iteration']
        self.protocol = data['protocol']
        if (type(self.iterations) is not int or not 1 <= self.iterations <= 1000000
                or not 1 <= len(self.salt) <= 1024 or self.protocol not in ('s2k', 's2k_fo')):
            raise ValueError('Invalid SRP challenge')

    def encode(self):
        digest = self.digest.hex().encode() if self.protocol == 's2k_fo' else self.digest
        return hashlib.pbkdf2_hmac('sha256', digest, self.salt, self.iterations, 32)


def merge_web_session(login_state, web_state):
    """Keep IDMS cookies while replacing the complete iCloud cookie snapshot."""
    state = copy.deepcopy(login_state or {})
    tokens = state.setdefault('tokens', {})
    for key in ('session_token', 'trust_token', 'account_country'):
        if key in web_state.get('tokens', {}):
            tokens[key] = web_state['tokens'][key]
    state['cookies'] = [c for c in state.get('cookies', [])
                        if c.get('domain', '').lstrip('.') in ('apple.com', 'idmsa.apple.com')]
    state['cookies'].extend(copy.deepcopy(web_state.get('cookies', [])))
    return state


class AppleLogin:
    def __init__(self, username, password, host='icloud.com', state=None):
        if host not in ('icloud.com', 'icloud.com.cn'):
            raise AppleLoginError('请选择有效的 iCloud 地区')
        self.username, self.password, self.host = username, password, host
        self.origin = 'https://www.' + host
        self.auth_url = 'https://idmsa.apple.com/appleauth/auth'
        self.setup_url = 'https://setup.' + host + '/setup/ws/1'
        self.session = requests.Session()
        configure_session(self.session, load_config())
        self.session.headers.update({'User-Agent': USER_AGENT, 'Origin': self.origin,
                                     'Referer': self.origin + '/'})
        state = state or {}
        self.tokens = {k: v for k, v in state.get('tokens', {}).items()
                       if k in TOKEN_HEADERS.values() and isinstance(v, str)}
        self.client_id = state.get('client_id') or 'auth-' + str(uuid.uuid4())
        for cookie in state.get('cookies', []):
            if not self._allowed_domain(cookie.get('domain', '')):
                continue
            expires = cookie.get('expires')
            if expires is not None and expires <= time.time():
                continue
            self.session.cookies.set(cookie['name'], cookie['value'],
                domain=cookie['domain'], path=cookie.get('path') or '/',
                secure=bool(cookie.get('secure', True)), expires=expires)
        self.phones = []
        self.phone_id = None
        self.code_method = 'device'
        self.data = {}
        self.reset_deadline()

    def reset_deadline(self):
        self.deadline = time.monotonic() + 45

    def close(self):
        self.password = ''
        self.session.close()

    def _allowed_domain(self, domain):
        domain = domain.lstrip('.').lower()
        return domain in ('apple.com', 'idmsa.apple.com') or domain == self.host or domain.endswith('.' + self.host)

    def headers(self):
        headers = {'Accept': 'application/json, text/javascript',
                   'Content-Type': 'application/json', 'Referer': 'https://idmsa.apple.com/',
                   'X-Apple-OAuth-Client-Id': WIDGET_KEY, 'X-Apple-Widget-Key': WIDGET_KEY,
                   'X-Apple-OAuth-Client-Type': 'firstPartyAuth',
                   'X-Apple-OAuth-Redirect-URI': self.origin,
                   'X-Apple-OAuth-Require-Grant-Code': 'true',
                   'X-Apple-OAuth-Response-Mode': 'web_message',
                   'X-Apple-OAuth-Response-Type': 'code',
                   'X-Apple-OAuth-State': self.client_id, 'X-Apple-Frame-Id': self.client_id,
                   'X-Apple-FD-Client-Info': json.dumps({
                       'U': USER_AGENT, 'L': 'en-US', 'Z': 'GMT+00:00', 'V': '1.1', 'F': ''}, separators=(',', ':'))}
        for header, key in TOKEN_HEADERS.items():
            if key in ('scnt', 'session_id', 'auth_attributes') and self.tokens.get(key):
                headers[header] = self.tokens[key]
        return headers

    def _request(self, method, url, *, allowed=(), **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AppleLoginError('网络异常：Apple 登录超时，请稍后重试', 'network')
        try:
            response = self.session.request(method, url, timeout=min(10, remaining),
                allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise AppleLoginError('网络异常：无法连接 Apple 登录服务，请稍后重试', 'network') from None
        for header, key in TOKEN_HEADERS.items():
            if response.headers.get(header):
                self.tokens[key] = response.headers[header]
        if response.status_code not in allowed and not 200 <= response.status_code < 300:
            if response.status_code == 429:
                raise AppleLoginError('Apple 暂时限制登录，请稍后再试', 'throttle')
            if response.status_code >= 500:
                raise AppleLoginError('网络异常：Apple 登录服务暂时不可用', 'network')
            raise AppleLoginError('Apple 拒绝登录，请检查账号密码；账号锁定或其他验证请到 Apple 网站完成')
        if 200 <= response.status_code < 300 and response.text:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and (body.get('serviceErrors') or body.get('error') or body.get('success') is False):
                raise AppleLoginError('Apple 拒绝登录，请检查密码或到 Apple 网站完成账号验证')
        return response

    @staticmethod
    def _json(response):
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise AppleLoginError('网络异常：Apple 登录响应无法解析', 'network') from None

    def start(self):
        self._request('GET', self.auth_url + '/authorize/signin', params={
            'frame_id': self.client_id, 'iframeid': self.client_id, 'skVersion': '7',
            'client_id': WIDGET_KEY, 'response_type': 'code', 'redirect_uri': self.origin,
            'response_mode': 'web_message', 'state': self.client_id, 'authVersion': 'latest'})
        password = _Password(self.password)
        user = srp.User(self.username, password, hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        username, public = user.start_authentication()
        response = self._request('POST', self.auth_url + '/signin/init',
            headers=self.headers(), json={'accountName': username,
            'a': base64.b64encode(public).decode(), 'protocols': ['s2k', 's2k_fo']})
        try:
            challenge = self._json(response)
            password.prepare(challenge)
            public_b = base64.b64decode(challenge['b'], validate=True)
            if not 1 <= len(public_b) <= 256:
                raise ValueError()
            proof = user.process_challenge(password.salt, public_b)
            if not proof or not user.H_AMK:
                raise ValueError()
            payload = {'accountName': username, 'c': challenge['c'],
                'm1': base64.b64encode(proof).decode(),
                'm2': base64.b64encode(user.H_AMK).decode(), 'rememberMe': True,
                'trustTokens': [self.tokens['trust_token']] if self.tokens.get('trust_token') else []}
        except (KeyError, TypeError, ValueError):
            raise AppleLoginError('Apple 登录协议响应异常，请稍后重试或使用 Cookie 导入') from None
        response = self._request('POST', self.auth_url + '/signin/complete', allowed=(409,),
            headers=self.headers(), params={'isRememberMeEnabled': 'true'}, json=payload)
        if response.status_code == 409:
            self._load_mfa_options()
            return False
        if self._account_login():
            return True
        self._load_mfa_options()
        return False

    def _load_mfa_options(self):
        response = self._request('GET', self.auth_url, headers=self.headers())
        data = self._json(response)
        if data.get('fsaChallenge') or data.get('keyNames'):
            raise AppleLoginError('这个账号要求安全密钥，请在 Apple 网站验证后使用 Cookie 导入')
        self.phones = []
        verification = data.get('phoneNumberVerification') or {}
        numbers = verification.get('trustedPhoneNumbers') or data.get('trustedPhoneNumbers') or []
        single = verification.get('trustedPhoneNumber')
        if single and not numbers:
            numbers = [single]
        for phone in numbers:
            if isinstance(phone, dict) and isinstance(phone.get('id'), (int, str)):
                number = str(phone.get('numberWithDialCode') or phone.get('number') or '')
                digits = ''.join(c for c in number if c.isdigit())
                self.phones.append({'id': str(phone['id']),
                                    'number': '•••• ' + digits[-2:] if digits else '可信手机号'})

    def send_sms(self, phone_id):
        if str(phone_id) not in {p['id'] for p in self.phones}:
            raise AppleLoginError('请选择 Apple 提供的可信手机号')
        self._request('PUT', self.auth_url + '/verify/phone', headers=self.headers(),
            json={'phoneNumber': {'id': int(phone_id) if str(phone_id).isdigit() else phone_id}, 'mode': 'sms'})
        self.phone_id, self.code_method = str(phone_id), 'sms'

    def verify(self, code, method='device'):
        if method not in ('device', 'sms') or (method == 'sms' and not self.phone_id):
            raise AppleLoginError('请先发送短信验证码，或选择可信设备验证码')
        data = {'securityCode': {'code': code}}
        path = '/verify/trusteddevice/securitycode'
        if method == 'sms':
            path = '/verify/phone/securitycode'
            data.update(phoneNumber={'id': int(self.phone_id) if self.phone_id.isdigit() else self.phone_id}, mode='sms')
        response = self._request('POST', self.auth_url + path, headers=self.headers(),
                                 json=data, allowed=(400, 401, 409))
        if response.text and 200 <= response.status_code < 300:
            if self._json(response).get('securityCode', {}).get('valid') is False:
                raise AppleLoginError('验证码无效或已过期，请重新获取验证码')
        if response.status_code in (400, 401) or (response.status_code == 409 and
                self._json(response).get('securityCode', {}).get('valid') is not True):
            raise AppleLoginError('验证码无效或已过期，请重新获取验证码')
        self._request('GET', self.auth_url + '/2sv/trust', headers=self.headers())
        if not self._account_login():
            raise AppleLoginError('Apple 仍要求验证，请到 Apple 网站检查账号状态')
        return True

    def _account_login(self):
        token = self.tokens.get('session_token')
        if not token:
            raise AppleLoginError('Apple 未提供会话令牌，请使用 Cookie 导入')
        response = self._request('POST', self.setup_url + '/accountLogin', json={
            'dsWebAuthToken': token, 'extended_login': True,
            'trustToken': self.tokens.get('trust_token', ''),
            'accountCountryCode': self.tokens.get('account_country')})
        self.data = self._json(response)
        info = self.data.get('dsInfo') or {}
        if self.data.get('hsaChallengeRequired') or (info.get('hsaVersion', 0) >= 1 and not self.data.get('hsaTrustedBrowser')):
            return False
        if self.data.get('termsUpdateNeeded') or self.data.get('error') or self.data.get('success') is False:
            raise AppleLoginError('请在 Apple 网站完成账号设置、协议确认或验证后重试')
        if not self.data.get('webservices', {}).get('premiummailsettings', {}).get('url'):
            raise AppleLoginError('此账号尚不能使用隐藏邮件地址，请检查 iCloud+ 和账号设置')
        identity = info.get('appleId') or info.get('appleIdEmail') or info.get('primaryEmail')
        if not identity or identity.lower() != self.username.lower():
            raise AppleLoginError('Apple 返回的账号身份与登录账号不一致，未保存凭据')
        return True

    def export_state(self):
        return {'version': 1, 'client_id': self.client_id, 'tokens': dict(self.tokens), 'cookies': [
            {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
             'secure': c.secure, 'expires': c.expires}
            for c in self.session.cookies if self._allowed_domain(c.domain) and not c.is_expired()]}

    def web_state(self):
        state = self.export_state()
        return {'version': 1, 'tokens': {k: v for k, v in self.tokens.items()
                if k in ('session_token', 'trust_token', 'account_country')},
                'cookies': [c for c in state['cookies'] if c['domain'].lstrip('.') not in ('apple.com', 'idmsa.apple.com')]}
