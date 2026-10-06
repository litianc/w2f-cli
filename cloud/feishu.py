"""Strict Feishu HTTP boundary. Unknown send outcomes are never reported as failures safe to replay."""
import http.client
import json
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.request


class DeliveryError(Exception):
    def __init__(self, code, outcome='retry', retry_after=5):
        super().__init__(code)
        self.code, self.outcome, self.retry_after = code, outcome, retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Feishu:
    def __init__(self, environment, credentials_file, base_url='https://open.feishu.cn', clock=time.time):
        if base_url != 'https://open.feishu.cn' and not (environment == 'v040-ci' and base_url == 'http://feishu-mock:8080'):
            raise ValueError('Only official Feishu or the isolated CI fixture is supported')
        self.credentials_file = Path(credentials_file)
        self.base_url, self.clock = base_url, clock
        self.mock = base_url.startswith('http:')
        self.cache = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def token(self, app_id, user_id):
        try:
            entries = json.loads(self.credentials_file.read_text(encoding='utf-8'))
            entry = entries[app_id]
            secret = entry['app_secret']
            if str(entry['user_id']) != str(user_id) or not isinstance(secret,str) or len(secret) < 12:
                raise ValueError()
            if self.mock and (not app_id.startswith('cli_verify_') or not secret.startswith('synthetic-')):
                raise ValueError()
        except (OSError,ValueError,KeyError,TypeError):
            raise DeliveryError('app_credentials_unavailable','failed') from None
        # Credential ownership is revalidated even on cache hits; no cross-user token reuse.
        cached = self.cache.get((app_id,secret))
        if cached and self.clock() < cached[1]:
            return cached[0]
        status, body, headers = self._request('/open-apis/auth/v3/tenant_access_token/internal',
                                            dict(app_id=app_id,app_secret=secret), send=False)
        if status != 200 or body.get('code') != 0:
            raise DeliveryError('app_auth_rejected','retry' if status >= 500 or status == 429 else 'failed')
        token, expire = body.get('tenant_access_token'), body.get('expire')
        if not isinstance(token,str) or not token or type(expire) is not int or expire <= 0:
            raise DeliveryError('invalid_auth_response')
        self.cache[(app_id,secret)] = (token,self.clock()+max(0,expire-60))
        return token

    def _request(self, path, payload, token=None, send=True):
        headers={'Content-Type':'application/json; charset=utf-8'}
        if token:
            headers['Authorization']='Bearer '+token
        request=urllib.request.Request(self.base_url+path,
            data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode(),headers=headers)
        try:
            try:
                response=self.opener.open(request,timeout=10)
            except urllib.error.HTTPError as exc:
                response=exc
            with response:
                status, headers=response.code,response.headers
                raw=response.read(1024*1024+1)
            if len(raw)>1024*1024:
                raise ValueError()
            body=json.loads(raw)
            if not isinstance(body,dict):
                raise ValueError()
            return status,body,headers
        except (OSError,urllib.error.URLError,http.client.HTTPException,ValueError):
            raise DeliveryError('send_response_unknown' if send else 'auth_unavailable',
                                'unknown' if send else 'retry') from None

    def send(self, payload, token):
        status,body,headers=self._request('/open-apis/im/v1/messages?receive_id_type=chat_id',payload,token)
        code=body.get('code')
        if 200 <= status < 300 and type(code) is int and code == 0:
            message_id=(body.get('data') or {}).get('message_id') if isinstance(body.get('data'),dict) else None
            if isinstance(message_id,str) and re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}',message_id):
                return message_id
            raise DeliveryError('send_receipt_missing','unknown')
        if status == 429 or (type(code) is int and code in {230020,99991400}):
            try:
                delay=max(1,min(3600,int(headers.get('Retry-After','5'))))
            except ValueError:
                delay=5
            raise DeliveryError('rate_limited','retry',delay)
        definite_rejections={230001,230002,230006,230013,230015,230017,230018,230019,230022,230025,230027,230028,230029,230034,230035,230036,230038,230053,230054,230055,230075,232009}
        if status >= 500 or type(code) is not int or code not in definite_rejections or status < 200 or 300 <= status < 400:
            raise DeliveryError('send_response_unknown','unknown')
        # A structured API rejection establishes only this attempt was rejected.
        raise DeliveryError('send_rejected','failed')
