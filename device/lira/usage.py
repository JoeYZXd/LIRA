"""Content-free device counters and read-only Volcengine account balance."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
import time

import httpx

LOCAL_TZ = timezone(timedelta(hours=8))
FIELDS = ('sessions', 'turns', 'input_seconds', 'zero_seconds', 'output_seconds')
META_KEY = 'doubao_daily_usage_v1'


class UsageLedger:
    def __init__(self, store, *, now=None):
        self.store = store
        self.now = now or (lambda: datetime.now(LOCAL_TZ))
        self._last_flush = time.monotonic()
        raw = store.get_meta(META_KEY)
        try:
            self.data = json.loads(raw) if raw else {}
            if not isinstance(self.data, dict): self.data = {}
        except (ValueError, TypeError):
            self.data = {}
        self.started_at = self.data.setdefault('started_at', self.now().isoformat())
        self.days = self.data.setdefault('days', {})

    def add(self, source, field, amount=1):
        if source not in ('normal','test') or field not in FIELDS:
            raise ValueError('Unknown usage counter')
        day = self.now().astimezone(LOCAL_TZ).date().isoformat()
        bucket = self.days.setdefault(day, {}).setdefault(source, {})
        bucket[field] = bucket.get(field, 0) + amount
        if time.monotonic()-self._last_flush >= 10:
            self.flush()

    def flush(self):
        # Keep three months of daily aggregates, no transcripts or identities.
        for day in sorted(self.days)[:-90]: del self.days[day]
        self.store.set_meta(META_KEY, json.dumps(self.data, separators=(',',':')))
        self._last_flush = time.monotonic()

    def snapshot(self):
        rows=[]
        today=self.now().astimezone(LOCAL_TZ).date()
        for offset in range(6,-1,-1):
            day=(today-timedelta(days=offset)).isoformat()
            row={'date':day}
            for source in ('normal','test'):
                row[source]={key:round(self.days.get(day,{}).get(source,{}).get(key,0),3)
                             for key in FIELDS}
            row['total']={key:round(row['normal'][key]+row['test'][key],3) for key in FIELDS}
            rows.append(row)
        return {'started_at':self.started_at,'timezone':'Asia/Shanghai','days':rows}


def balance_headers(access_key, secret_key, now=None):
    """Volcengine HMAC-SHA256 V4, GET QueryBalanceAcct (no request body)."""
    stamp=(now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%SZ')
    body_hash=hashlib.sha256(b'').hexdigest()
    host='billing.volcengineapi.com'
    names='host;x-content-sha256;x-date'
    canonical='\n'.join(['GET','/','Action=QueryBalanceAcct&Version=2022-01-01',
                         f'host:{host}\nx-content-sha256:{body_hash}\nx-date:{stamp}\n',
                         names,body_hash])
    scope=f'{stamp[:8]}/cn-north-1/billing/request'
    to_sign='\n'.join(['HMAC-SHA256',stamp,scope,hashlib.sha256(canonical.encode()).hexdigest()])
    key=secret_key.encode()
    for part in (stamp[:8],'cn-north-1','billing','request'):
        key=hmac.new(key,part.encode(),hashlib.sha256).digest()
    signature=hmac.new(key,to_sign.encode(),hashlib.sha256).hexdigest()
    return {'Host':host,'X-Date':stamp,'X-Content-Sha256':body_hash,
            'Authorization':f'HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={names}, Signature={signature}'}


class AccountBalance:
    def __init__(self, *, env=None, transport=None):
        env=os.environ if env is None else env
        self._ak=env.get('VOLCENGINE_ACCESS_KEY_ID','').strip()
        self._sk=env.get('VOLCENGINE_SECRET_ACCESS_KEY','').strip()
        self.transport=transport
        self._lock=asyncio.Lock()
        self._last_attempt=0
        self._value=None
        self._updated=None
        self._error=''

    async def snapshot(self, *, refresh=False):
        configured=bool(self._ak and self._sk)
        if configured:
            async with self._lock:
                interval=60 if refresh or self._error else 300
                if not self._last_attempt or time.monotonic()-self._last_attempt>=interval:
                    self._last_attempt=time.monotonic()
                    try:
                        async with httpx.AsyncClient(timeout=10,transport=self.transport,
                                                     follow_redirects=False) as client:
                            response=await client.get('https://billing.volcengineapi.com/',
                                params={'Action':'QueryBalanceAcct','Version':'2022-01-01'},
                                headers=balance_headers(self._ak,self._sk))
                            response.raise_for_status()
                            data=response.json()
                        if not isinstance(data,dict) or not isinstance(data.get('ResponseMetadata',{}),dict):
                            raise ValueError('Invalid provider response')
                        if data.get('ResponseMetadata',{}).get('Error'):
                            raise ValueError('Provider error')
                        result=data['Result']
                        if not isinstance(result,dict):raise ValueError('Invalid amounts')
                        value={}
                        for name in ('AvailableBalance','CashBalance','ArrearsBalance'):
                            number=Decimal(str(result[name]))
                            if not number.is_finite():raise ValueError('Invalid amount')
                            value[name]=str(number)
                        self._value=value
                        self._updated=datetime.now(LOCAL_TZ).isoformat()
                        self._error=''
                    except httpx.HTTPStatusError as exc:
                        try: code=exc.response.json().get('ResponseMetadata',{}).get('Error',{}).get('Code')
                        except (ValueError,AttributeError): code=None
                        self._error=({'InvalidAccessKey':'余额查询密钥无效，请核对并启用 IAM Access Key ID。',
                            'SignatureDoesNotMatch':'余额签名校验失败，请核对 AK/SK 配对及设备时间。'}.get(code) or
                            ('余额查询无权限，请检查子账号费用中心查询权限。' if exc.response.status_code in (401,403)
                             else '费用中心查询失败，请稍后刷新。'))
                    except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation):
                        self._error='余额查询失败，请检查网络、凭据及费用中心权限。'
        return {'configured':configured,'amounts':self._value,'updated_at':self._updated,
                'stale':bool(self._error and self._value),'error':self._error,
                'low_balance':self._value is not None and Decimal(self._value['AvailableBalance'])<Decimal('50'),
                'refresh_seconds':300}
